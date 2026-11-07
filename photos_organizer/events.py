"""Suggest event and trip albums from photo dates and locations."""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from statistics import median


@dataclass
class EventSettings:
    gap_hours: float = 6.0        # a pause this long starts a new event
    min_items: int = 10           # smaller bursts are not worth an album
    away_km: float = 100.0        # this far from home counts as travel
    trip_gap_hours: float = 36.0  # travel days this close together form one trip
    already_in_album: float = 0.8 # skip suggestions already this covered by one of your albums


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def date_range(start: float, end: float) -> str:
    a, b = datetime.fromtimestamp(start), datetime.fromtimestamp(end)
    if a.date() == b.date():
        return f"{a.day} {a:%b %Y}"
    if (a.year, a.month) == (b.year, b.month):
        return f"{a.day}–{b.day} {b:%b %Y}"
    if a.year == b.year:
        return f"{a.day} {a:%b} – {b.day} {b:%b %Y}"
    return f"{a.day} {a:%b %Y} – {b.day} {b:%b %Y}"


def place_names(points: list[tuple[float, float]]) -> list[str | None]:
    """City names for coordinates, offline, when the optional reverse_geocoder is installed."""
    if not points:
        return []
    try:
        import reverse_geocoder
    except ImportError:
        return [None] * len(points)
    return [r.get("name") or r.get("admin1") for r in reverse_geocoder.search(points, mode=1, verbose=False)]


@dataclass
class Segment:
    items: list[sqlite3.Row]

    @property
    def start(self) -> float:
        return self.items[0]["taken_at"]

    @property
    def end(self) -> float:
        return self.items[-1]["taken_at"]

    @property
    def centre(self) -> tuple[float, float] | None:
        geo = [(it["lat"], it["lon"]) for it in self.items if it["lat"] is not None]
        return (median(p[0] for p in geo), median(p[1] for p in geo)) if geo else None


def home_location(items: list[sqlite3.Row]) -> tuple[float, float] | None:
    cells = Counter((round(it["lat"], 1), round(it["lon"], 1)) for it in items if it["lat"] is not None)
    return cells.most_common(1)[0][0] if cells else None


def segments(items: list[sqlite3.Row], s: EventSettings) -> list[Segment]:
    out: list[Segment] = []
    for it in items:
        if out and it["taken_at"] - out[-1].end <= s.gap_hours * 3600:
            out[-1].items.append(it)
        else:
            out.append(Segment([it]))
    return out


def suggest(items: list[sqlite3.Row], s: EventSettings) -> list[tuple[str, list[Segment]]]:
    """Group time-sorted items into ('event' | 'trip', segments) suggestions."""
    home = home_location(items)

    def away(seg: Segment) -> bool:
        c = seg.centre
        return home is not None and c is not None and haversine_km(c, home) > s.away_km

    out: list[tuple[str, list[Segment]]] = []
    for seg in segments(items, s):
        if (
            away(seg)
            and out
            and out[-1][0] == "trip"
            and seg.start - out[-1][1][-1].end <= s.trip_gap_hours * 3600
        ):
            out[-1][1].append(seg)
        elif away(seg):
            out.append(("trip", [seg]))
        else:
            out.append(("event", [seg]))
    return [(kind, segs) for kind, segs in out if sum(len(x.items) for x in segs) >= s.min_items]


def build(conn: sqlite3.Connection, s: EventSettings | None = None) -> dict[str, int]:
    s = s or EventSettings()
    conn.execute("DELETE FROM event_items")
    conn.execute("DELETE FROM events")
    items = conn.execute("SELECT * FROM items WHERE taken_at IS NOT NULL ORDER BY taken_at").fetchall()
    membership: dict[int, set[int]] = {}
    for r in conn.execute("SELECT album_id, item_id FROM album_items"):
        membership.setdefault(r["item_id"], set()).add(r["album_id"])

    found = suggest(items, s)
    centres = [Segment([it for seg in segs for it in seg.items]).centre for _, segs in found]
    with_geo = [i for i, c in enumerate(centres) if c]
    places = dict(zip(with_geo, place_names([centres[i] for i in with_geo])))
    stats = {"events": 0, "trips": 0, "skipped_already_in_album": 0}

    for n, (kind, segs) in enumerate(found):
        members = [it for seg in segs for it in seg.items]
        albums = Counter(a for it in members for a in membership.get(it["id"], ()))
        if albums and albums.most_common(1)[0][1] >= s.already_in_album * len(members):
            stats["skipped_already_in_album"] += 1
            continue
        place = places.get(n)
        when = date_range(segs[0].start, segs[-1].end)
        title = f"{place}, {when}" if place else f"{'Trip' if kind == 'trip' else 'Event'}, {when}"
        eid = conn.execute(
            "INSERT INTO events(title, kind, start_at, end_at) VALUES (?, ?, ?, ?)",
            (title, kind, segs[0].start, segs[-1].end),
        ).lastrowid
        conn.executemany(
            "INSERT INTO event_items(event_id, item_id) VALUES (?, ?)", [(eid, it["id"]) for it in members]
        )
        stats["trips" if kind == "trip" else "events"] += 1
    conn.commit()
    return stats
