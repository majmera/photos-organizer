"""Cached JPEG thumbnails for the review page and the cleanup lists."""

from __future__ import annotations

from pathlib import Path

from .analyze import load


def thumbnail(src: str, item_id: int, side: int, cache_dir: Path) -> Path:
    out = cache_dir / f"{item_id}_{side}.jpg"
    if not out.exists():
        cache_dir.mkdir(parents=True, exist_ok=True)
        img, _ = load(src)
        img.thumbnail((side, side))
        img.save(out, "JPEG", quality=82)
    return out
