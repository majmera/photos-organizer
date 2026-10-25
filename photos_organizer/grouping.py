"""Find exact duplicates and near-duplicate bursts, and propose which shots to keep."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Settings:
    exact_phash: int = 4          # max pHash distance (of 64 bits) for "same picture"
    exact_dhash: int = 6
    near_window: float = 60.0     # seconds between shots in one burst
    near_phash: int = 20          # max pHash distance for "same scene"
    keep: int = 2                 # most shots kept per near-duplicate group
    second_within: float = 0.10   # a 2nd+ shot is kept only if within 10% of the best score
    weights: dict = field(default_factory=lambda: {
        "sharpness": 0.45, "eyes": 0.25, "exposure": 0.15, "framing": 0.15,
    })


class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, a: int) -> int:
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)

    def groups(self) -> list[list[int]]:
        out: dict[int, list[int]] = {}
        for i in range(len(self.parent)):
            out.setdefault(self.find(i), []).append(i)
        return [g for g in out.values() if len(g) > 1]


def hamming_matrix(rows: np.ndarray, cols: np.ndarray) -> np.ndarray:
    return np.bitwise_count(rows[:, None] ^ cols[None, :])


def to_bits(hexes: list[str]) -> np.ndarray:
    return np.array([int(h, 16) for h in hexes], dtype=np.uint64)


def exact_groups(items: list[sqlite3.Row], s: Settings) -> list[list[int]]:
    """Indexes into `items` of copies of the same picture (re-saved, resized, metadata stripped)."""
    n = len(items)
    uf = UnionFind(n)
    by_pixels: dict[str, int] = {}
    for i, it in enumerate(items):
        if it["pixel_hash"] in by_pixels:
            uf.union(i, by_pixels[it["pixel_hash"]])
        else:
            by_pixels[it["pixel_hash"]] = i

    ph, dh = to_bits([it["phash"] for it in items]), to_bits([it["dhash"] for it in items])
    aspect = np.array([it["width"] / it["height"] for it in items])
    for start in range(0, n, 1000):
        stop = min(n, start + 1000)
        close = (
            (hamming_matrix(ph[start:stop], ph) <= s.exact_phash)
            & (hamming_matrix(dh[start:stop], dh) <= s.exact_dhash)
            & (np.abs(aspect[start:stop, None] / aspect[None, :] - 1) < 0.02)
        )
        for a, b in zip(*np.nonzero(close)):
            if start + a < b and is_copy(items[start + int(a)], items[int(b)], s):
                uf.union(start + int(a), int(b))
    return uf.groups()


def is_copy(a: sqlite3.Row, b: sqlite3.Row, s: Settings) -> bool:
    """Tell a re-saved copy from a burst shot that merely looks alike.

    Hashes can't see focus, so a blurred retake hashes like the sharp one. Two same-size
    photos taken a few seconds apart are a burst, and are left to the near-duplicate pass
    where quality decides; same-size copies normally share a timestamp.
    """
    if (a["width"], a["height"]) != (b["width"], b["height"]):
        return True
    if a["taken_at"] is None or b["taken_at"] is None:
        return True
    dt = abs(a["taken_at"] - b["taken_at"])
    return dt < 1 or dt > s.near_window


def copy_rank(it: sqlite3.Row) -> tuple:
    """Best copy of a duplicate: most pixels, then GPS, then a date, then the larger file."""
    return (it["width"] * it["height"], it["lat"] is not None, it["taken_at"] is not None, it["size"])


def near_groups(items: list[sqlite3.Row], s: Settings) -> list[list[int]]:
    """Indexes into `items` of shots of the same scene taken within `near_window` seconds."""
    order = sorted((i for i, it in enumerate(items) if it["taken_at"] is not None), key=lambda i: items[i]["taken_at"])
    ph = {i: int(items[i]["phash"], 16) for i in order}
    uf = UnionFind(len(items))
    for pos, i in enumerate(order):
        for j in order[pos + 1 :]:
            if items[j]["taken_at"] - items[i]["taken_at"] > s.near_window:
                break
            if (ph[i] ^ ph[j]).bit_count() <= s.near_phash:
                uf.union(i, j)
    return uf.groups()


def quality_scores(group: list[sqlite3.Row], s: Settings) -> list[float]:
    """Score each shot 0..1 relative to the others in its group."""
    sharp = [math.log1p(it["sharpness"] or 0) for it in group]
    top = max(sharp) or 1.0
    has_faces = any(it["faces"] for it in group)
    w = dict(s.weights)
    if not has_faces:
        w.pop("eyes")
    total = sum(w.values())
    scores = []
    for it, sh in zip(group, sharp):
        parts = {
            "sharpness": sh / top,
            "exposure": it["exposure"] or 0,
            "framing": it["framing"] if it["framing"] is not None else 1.0,
            # A shot where no face was found, in a group with faces, likely has people looking away.
            "eyes": it["eyes_open"] if it["eyes_open"] is not None else 0.0,
        }
        scores.append(sum(w[k] * parts[k] for k in w) / total)
    return scores


def build(conn: sqlite3.Connection, s: Settings | None = None) -> dict[str, int]:
    s = s or Settings()
    conn.execute("DELETE FROM group_items")
    conn.execute("DELETE FROM groups")
    items = conn.execute(
        "SELECT * FROM items WHERE kind = 'photo' AND analyzed = 1 AND error IS NULL ORDER BY id"
    ).fetchall()
    stats = {"exact_groups": 0, "exact_extra_copies": 0, "near_groups": 0, "near_discards": 0}
    if not items:
        conn.commit()
        return stats

    dropped: set[int] = set()
    for g in exact_groups(items, s):
        ranked = sorted(g, key=lambda i: copy_rank(items[i]), reverse=True)
        gid = conn.execute("INSERT INTO groups(kind) VALUES ('exact')").lastrowid
        for rank, i in enumerate(ranked):
            conn.execute(
                "INSERT INTO group_items(group_id, item_id, score, rank, keep) VALUES (?, ?, NULL, ?, ?)",
                (gid, items[i]["id"], rank, int(rank == 0)),
            )
        dropped.update(ranked[1:])
        stats["exact_groups"] += 1
        stats["exact_extra_copies"] += len(ranked) - 1

    # Near-duplicates are found among the copies that survive the exact pass.
    survivors = [it for i, it in enumerate(items) if i not in dropped]
    for g in near_groups(survivors, s):
        group = [survivors[i] for i in g]
        scores = quality_scores(group, s)
        ranked = sorted(range(len(group)), key=lambda k: scores[k], reverse=True)
        best = scores[ranked[0]]
        gid = conn.execute("INSERT INTO groups(kind) VALUES ('near')").lastrowid
        for rank, k in enumerate(ranked):
            keep = rank == 0 or (rank < s.keep and scores[k] >= best * (1 - s.second_within))
            conn.execute(
                "INSERT INTO group_items(group_id, item_id, score, rank, keep) VALUES (?, ?, ?, ?, ?)",
                (gid, group[k]["id"], round(scores[k], 4), rank, int(keep)),
            )
            stats["near_discards"] += not keep
        stats["near_groups"] += 1
    conn.commit()
    return stats
