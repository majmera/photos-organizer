import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFilter


def scene(seed: int, size=(1600, 1200)) -> Image.Image:
    """A synthetic photo: large smooth shapes (what the hashes see) plus fine detail (what focus sees)."""
    rng = np.random.default_rng(seed)
    coarse = Image.fromarray(rng.integers(0, 255, (12, 16, 3), dtype=np.uint8)).resize(size, Image.BICUBIC)
    fine = rng.normal(0, 28, (size[1], size[0], 1))
    arr = np.clip(np.asarray(coarse, dtype=float) + fine, 0, 255).astype(np.uint8)
    return Image.fromarray(arr)


def sidecar(path: Path, taken: float, lat=0.0, lon=0.0, name: str | None = None) -> None:
    data = {
        "title": path.name,
        "photoTakenTime": {"timestamp": str(int(taken))},
        "geoData": {"latitude": lat, "longitude": lon},
    }
    (path.parent / (name or path.name + ".supplemental-metadata.json")).write_text(json.dumps(data))


@pytest.fixture
def takeout_dir(tmp_path: Path) -> Path:
    return build_takeout(tmp_path)


def build_takeout(tmp_path: Path) -> Path:
    """A small Takeout tree covering duplicates, a burst, an edited copy and an album."""
    root = tmp_path / "Takeout" / "Google Photos"
    year = root / "Photos from 2024"
    album = root / "Goa trip"
    year.mkdir(parents=True)
    album.mkdir()
    (album / "metadata.json").write_text(json.dumps({"title": "Goa trip"}))

    t0 = 1_717_000_000  # 29 May 2024
    # A burst: one sharp shot, two progressively blurred ones.
    sharp = scene(1)
    sharp.save(year / "burst_1.jpg", quality=92)
    sharp.filter(ImageFilter.GaussianBlur(3)).save(year / "burst_2.jpg", quality=92)
    sharp.filter(ImageFilter.GaussianBlur(6)).save(year / "burst_3.jpg", quality=92)
    for i in (1, 2, 3):
        sidecar(year / f"burst_{i}.jpg", t0 + i * 4)

    # An unrelated photo an hour later.
    scene(2).save(year / "other.jpg", quality=92)
    sidecar(year / "other.jpg", t0 + 3600)

    # The same picture twice: an original and a smaller re-saved copy (e.g. from WhatsApp).
    original = scene(3)
    original.save(year / "IMG_0001.jpg", quality=95)
    sidecar(year / "IMG_0001.jpg", t0 + 7200, lat=15.5, lon=73.8)
    original.resize((800, 600)).save(year / "IMG-WA0001.jpg", quality=70)
    sidecar(year / "IMG-WA0001.jpg", t0 + 90_000, name="IMG-WA0001.jpg.json")

    # The original also sits in an album (same bytes), and has an edited copy.
    (album / "IMG_0001.jpg").write_bytes((year / "IMG_0001.jpg").read_bytes())
    sidecar(album / "IMG_0001.jpg", t0 + 7200, lat=15.5, lon=73.8)
    original.rotate(2).save(year / "IMG_0001-edited.jpg", quality=95)

    # A clashing filename: "IMG_0002(1).jpg" pairs with "IMG_0002.jpg(1).json".
    scene(4).save(year / "IMG_0002(1).jpg", quality=92)
    sidecar(year / "IMG_0002(1).jpg", t0 + 20_000, name="IMG_0002.jpg(1).json")
    return tmp_path
