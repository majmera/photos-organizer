"""Per-photo analysis: content hashes and quality signals.

Everything runs locally on the CPU. Results are stored on the item, so re-runs only
analyze photos that are new.
"""

from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime

import cv2
import imagehash
import numpy as np
from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # HEIC files will then fail to open and be reported
    pass

ANALYSIS_SIDE = 1024  # quality signals are measured at this resolution
EXIF_DATETIME_ORIGINAL = 0x9003
EXIF_IFD = 0x8769

_face = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
_eye = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye_tree_eyeglasses.xml")


@dataclass
class Analysis:
    width: int
    height: int
    pixel_hash: str
    phash: str
    dhash: str
    sharpness: float
    exposure: float
    faces: int
    eyes_open: float | None
    framing: float
    exif_taken_at: float | None


def load(path: str) -> tuple[Image.Image, float | None]:
    img = Image.open(path)
    taken = None
    try:
        raw = img.getexif().get_ifd(EXIF_IFD).get(EXIF_DATETIME_ORIGINAL)
        if raw:
            taken = datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S").timestamp()
    except (ValueError, KeyError, AttributeError):
        pass
    img = ImageOps.exif_transpose(img).convert("RGB")
    return img, taken


def sharpness(gray: np.ndarray, boxes: list[tuple[int, int, int, int]]) -> float:
    """Laplacian variance of the sharpest part of the frame.

    With faces, the faces are what must be in focus. Without, the 90th percentile of a
    6x6 tile grid, so a sharp subject on a soft background still scores as sharp.
    """
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    if boxes:
        return float(max(lap[y : y + h, x : x + w].var() for x, y, w, h in boxes))
    h, w = gray.shape
    tiles = [
        lap[r * h // 6 : (r + 1) * h // 6, c * w // 6 : (c + 1) * w // 6].var()
        for r in range(6)
        for c in range(6)
    ]
    return float(np.percentile(tiles, 90))


def exposure(gray: np.ndarray) -> float:
    """1.0 for a well-exposed frame, falling towards 0 for dark, blown-out or clipped frames."""
    mean = gray.mean() / 255
    clipped = (gray < 5).mean() + (gray > 250).mean()
    return float(np.clip(1 - abs(mean - 0.5) * 1.2 - max(0.0, clipped - 0.02) * 1.5, 0, 1))


def faces_and_eyes(gray: np.ndarray) -> tuple[list[tuple[int, int, int, int]], float | None]:
    side = min(gray.shape)
    min_face = max(24, side // 25)
    boxes = _face.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(min_face, min_face))
    boxes = [tuple(int(v) for v in b) for b in boxes]
    if not boxes:
        return [], None
    open_count = 0
    for x, y, w, h in boxes:
        upper = gray[y : y + int(h * 0.6), x : x + w]
        eyes = _eye.detectMultiScale(upper, scaleFactor=1.1, minNeighbors=4, minSize=(max(8, w // 10),) * 2)
        open_count += len(eyes) >= 2
    return boxes, open_count / len(boxes)


def framing(shape: tuple[int, int], boxes: list[tuple[int, int, int, int]]) -> float:
    """Share of faces that are fully inside the frame (1.0 when there are no faces)."""
    if not boxes:
        return 1.0
    h, w = shape
    margin = 0.015 * max(h, w)
    inside = sum(
        x > margin and y > margin and x + bw < w - margin and y + bh < h - margin
        for x, y, bw, bh in boxes
    )
    return inside / len(boxes)


def analyze_file(path: str) -> Analysis:
    img, exif_taken = load(path)
    pixel_hash = hashlib.sha1(img.tobytes()).hexdigest()
    ph, dh = str(imagehash.phash(img)), str(imagehash.dhash(img))
    small = img.copy()
    small.thumbnail((ANALYSIS_SIDE, ANALYSIS_SIDE))
    gray = cv2.cvtColor(np.asarray(small), cv2.COLOR_RGB2GRAY)
    boxes, eyes = faces_and_eyes(gray)
    return Analysis(
        width=img.width,
        height=img.height,
        pixel_hash=pixel_hash,
        phash=ph,
        dhash=dh,
        sharpness=sharpness(gray, boxes),
        exposure=exposure(gray),
        faces=len(boxes),
        eyes_open=eyes,
        framing=framing(gray.shape, boxes),
        exif_taken_at=exif_taken,
    )


def _worker(job: tuple[int, str]) -> tuple[int, dict | None, str | None]:
    item_id, path = job
    try:
        return item_id, asdict(analyze_file(path)), None
    except Exception as exc:  # one unreadable file must not stop the run
        return item_id, None, f"{type(exc).__name__}: {exc}"


def analyze(conn: sqlite3.Connection, workers: int | None = None, progress=print) -> dict[str, int]:
    jobs = [
        (r["id"], r["path"])
        for r in conn.execute("SELECT id, path FROM items WHERE kind = 'photo' AND analyzed = 0")
    ]
    stats = {"analyzed": 0, "errors": 0}
    if not jobs:
        return stats
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for n, (item_id, result, error) in enumerate(pool.map(_worker, jobs, chunksize=8), 1):
            if error:
                conn.execute("UPDATE items SET analyzed = 1, error = ? WHERE id = ?", (error, item_id))
                stats["errors"] += 1
            else:
                exif_taken = result.pop("exif_taken_at")
                cols = ", ".join(f"{k} = :{k}" for k in result)
                conn.execute(
                    f"UPDATE items SET {cols}, analyzed = 1, error = NULL,"
                    " taken_at = COALESCE(taken_at, :exif_taken) WHERE id = :id",
                    {**result, "exif_taken": exif_taken, "id": item_id},
                )
                stats["analyzed"] += 1
            if n % 200 == 0:
                progress(f"  analyzed {n}/{len(jobs)} photos")
                conn.commit()
    conn.commit()
    return stats
