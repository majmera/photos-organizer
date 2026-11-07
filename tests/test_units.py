from datetime import datetime

from photos_organizer import events
from photos_organizer.takeout import edited_original, match_sidecar


def test_sidecar_names():
    side = {
        "a.jpg": {"n": 1},
        "b.jpg.supplemental-metadata": {"n": 2},
        "c.jpg(1)": {"n": 3},
        "d.jpg.supplemental-metadata(2)": {"n": 4},
        "PXL_20240101_123456789.NIGHT.jpg.supplemen": {"n": 5},
        "x": {"title": "renamed.png", "n": 6},
    }
    assert match_sidecar("a.jpg", side)["n"] == 1
    assert match_sidecar("b.jpg", side)["n"] == 2
    assert match_sidecar("c(1).jpg", side)["n"] == 3
    assert match_sidecar("d(2).jpg", side)["n"] == 4
    assert match_sidecar("PXL_20240101_123456789.NIGHT.jpg", side)["n"] == 5
    assert match_sidecar("renamed.png", side)["n"] == 6
    assert match_sidecar("missing.jpg", side) is None


def test_edited_names():
    assert edited_original("IMG_1-edited.jpg") == "IMG_1.jpg"
    assert edited_original("IMG_1-bearbeitet.JPG") == "IMG_1.JPG"
    assert edited_original("IMG_1.jpg") is None


def item(i, when, lat=None, lon=None):
    return {"id": i, "taken_at": when.timestamp(), "lat": lat, "lon": lon}


def test_events_and_trips():
    home = (52.37, 4.90)  # Amsterdam
    away = (38.72, -9.14)  # Lisbon
    rows = []
    # Ten weekend outings at home make home the most common place.
    for d in range(1, 11):
        rows += [item(len(rows), datetime(2024, 3, d, 12, m), *home) for m in range(12)]
    # Three days in Lisbon, one evening of photos per day.
    for d in (20, 21, 22):
        rows += [item(len(rows), datetime(2024, 3, d, 19, m), *away) for m in range(5)]
    found = events.suggest(rows, events.EventSettings())
    kinds = [k for k, _ in found]
    assert kinds.count("trip") == 1
    trip = next(segs for k, segs in found if k == "trip")
    assert sum(len(s.items) for s in trip) == 15
    assert kinds.count("event") == 10


def test_date_range():
    a, b = datetime(2024, 3, 20, 10).timestamp(), datetime(2024, 3, 22, 18).timestamp()
    assert events.date_range(a, b) == "20–22 Mar 2024"
