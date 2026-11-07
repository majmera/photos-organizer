"""Read an extracted Google Takeout export of Google Photos into the index.

Takeout quirks handled here:
- A photo is written once per album it is in, plus once in a "Photos from YYYY" folder.
  Copies with identical bytes are merged into one item with several album memberships.
- Each file has a JSON sidecar named "<file>.json" or "<file>.supplemental-metadata.json",
  truncated when long, with "(n)" moved to the end for clashing names: "IMG(1).jpg" pairs
  with "IMG.jpg(1).json".
- Edited versions sit next to the original as "<name>-edited.<ext>". They are not separate
  items in Google Photos, so they are attached to their original instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

PHOTO_EXTS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".bmp", ".tif", ".tiff",
    ".dng", ".cr2", ".nef", ".arw", ".raf", ".orf", ".rw2",
}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".3gp", ".mkv", ".mts", ".m2ts", ".webm", ".wmv"}

# "-edited" in the languages Takeout is most often exported in.
EDITED_SUFFIXES = ("-edited", "-bearbeitet", "-modifié", "-editado", "-modificato", "-bewerkt")

YEAR_FOLDER = re.compile(r"^Photos from \d{4}$")
SKIP_FOLDERS = {"Trash", "Bin", "Failed Videos"}
SIDECAR_SUFFIX = ".supplemental-metadata"
COUNTER = re.compile(r"^(?P<stem>.*)\((?P<n>\d+)\)(?P<ext>\.[^.]+)$")
TRAILING_COUNTER = re.compile(r"\(\d+\)$")
# Takeout keeps sidecar names (including ".json") to about 51 characters.
TRUNCATED_LEN = 46


@dataclass
class Sidecar:
    title: str | None
    taken_at: float | None
    lat: float | None
    lon: float | None


def media_kind(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in PHOTO_EXTS:
        return "photo"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def parse_sidecar(data: dict) -> Sidecar:
    taken = None
    for key in ("photoTakenTime", "creationTime"):
        ts = (data.get(key) or {}).get("timestamp")
        if ts:
            taken = float(ts)
            break
    lat = lon = None
    for key in ("geoData", "geoDataExif"):
        geo = data.get(key) or {}
        if geo.get("latitude") or geo.get("longitude"):
            lat, lon = float(geo["latitude"]), float(geo["longitude"])
            break
    return Sidecar(title=data.get("title"), taken_at=taken, lat=lat, lon=lon)


def edited_original(name: str) -> str | None:
    """'IMG_1-edited.jpg' -> 'IMG_1.jpg'; None when the name is not an edited copy."""
    stem, ext = os.path.splitext(name)
    for suffix in EDITED_SUFFIXES:
        if stem.lower().endswith(suffix):
            return stem[: -len(suffix)] + ext
    return None


def match_sidecar(name: str, sidecars: dict[str, dict]) -> dict | None:
    """Find the sidecar for media file `name` among the JSON files of its folder.

    `sidecars` maps a JSON filename without ".json" to its parsed contents.
    """
    m = COUNTER.match(name)
    base, counter = (m["stem"] + m["ext"], f"({m['n']})") if m else (name, "")

    for stem in (base, base + SIDECAR_SUFFIX):
        if stem + counter in sidecars:
            return sidecars[stem + counter]

    # Truncated names: the sidecar stem is a cut-off prefix of "<base>.supplemental-metadata".
    full = base + SIDECAR_SUFFIX
    for stem, data in sidecars.items():
        if counter:
            if not stem.endswith(counter):
                continue
            stem = stem[: -len(counter)]
        elif TRAILING_COUNTER.search(stem):
            continue
        if full.startswith(stem) and len(stem) + len(".json") >= TRUNCATED_LEN:
            return data

    titled = [d for d in sidecars.values() if d.get("title") == name]
    return titled[0] if len(titled) == 1 else None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def album_title(folder: Path) -> str | None:
    """Album name for a Takeout folder, or None for year folders and the Trash."""
    if YEAR_FOLDER.match(folder.name) or folder.name in SKIP_FOLDERS:
        return None
    meta = folder / "metadata.json"
    if meta.exists():
        try:
            title = json.loads(meta.read_text(encoding="utf-8")).get("title")
            if title:
                return title
        except (OSError, ValueError):
            pass
    return folder.name


def find_photos_root(root: Path) -> Path:
    for candidate in (root / "Takeout" / "Google Photos", root / "Google Photos"):
        if candidate.is_dir():
            return candidate
    return root


def scan(conn: sqlite3.Connection, root: Path, progress=print) -> dict[str, int]:
    """Index every media file under `root`. Safe to re-run: known paths are skipped."""
    photos_root = find_photos_root(root)
    known = {r["path"]: r["item_id"] for r in conn.execute("SELECT path, item_id FROM files")}
    stats = {"files": 0, "new_items": 0, "album_copies": 0, "edited": 0, "skipped": 0}

    for dirpath, dirnames, filenames in os.walk(photos_root):
        folder = Path(dirpath)
        if folder.name in SKIP_FOLDERS:
            dirnames[:] = []
            continue
        dirnames.sort()
        sidecars = {}
        for fn in filenames:
            if fn.lower().endswith(".json") and fn != "metadata.json":
                try:
                    sidecars[fn[: -len(".json")]] = json.loads((folder / fn).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
        album = album_title(folder) if folder != photos_root else None
        album_id = None
        if album:
            conn.execute("INSERT OR IGNORE INTO albums(title) VALUES (?)", (album,))
            album_id = conn.execute("SELECT id FROM albums WHERE title = ?", (album,)).fetchone()["id"]

        media = sorted(fn for fn in filenames if media_kind(Path(fn)))
        # Originals first, so edited copies can find them.
        media.sort(key=lambda fn: edited_original(fn) is not None)
        by_name: dict[str, int] = {}

        for fn in media:
            path = folder / fn
            spath = str(path.resolve())
            if spath in known:
                by_name[fn] = known[spath]
                stats["skipped"] += 1
                continue
            stats["files"] += 1

            original = edited_original(fn)
            if original and original in by_name:
                conn.execute("UPDATE items SET edited_path = ? WHERE id = ?", (spath, by_name[original]))
                conn.execute("INSERT OR IGNORE INTO files(path, item_id) VALUES (?, ?)", (spath, by_name[original]))
                stats["edited"] += 1
                continue

            digest = sha256_file(path)
            row = conn.execute("SELECT id FROM items WHERE sha256 = ?", (digest,)).fetchone()
            if row:
                item_id = row["id"]
                stats["album_copies"] += 1
            else:
                side = parse_sidecar(match_sidecar(fn, sidecars) or {})
                cur = conn.execute(
                    "INSERT INTO items(sha256, path, filename, kind, size, taken_at, lat, lon)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (digest, spath, side.title or fn, media_kind(path), path.stat().st_size,
                     side.taken_at, side.lat, side.lon),
                )
                item_id = cur.lastrowid
                stats["new_items"] += 1
            by_name[fn] = item_id
            conn.execute("INSERT OR IGNORE INTO files(path, item_id) VALUES (?, ?)", (spath, item_id))
            if album_id:
                conn.execute("INSERT OR IGNORE INTO album_items(album_id, item_id) VALUES (?, ?)", (album_id, item_id))
            if stats["files"] % 500 == 0:
                progress(f"  scanned {stats['files']} files")
                conn.commit()
    conn.commit()
    return stats
