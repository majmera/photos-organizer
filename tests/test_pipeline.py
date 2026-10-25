import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from photos_organizer import analyze, db, events, export, grouping, takeout
from photos_organizer.review.server import make_handler


@pytest.fixture
def scanned(takeout_dir, tmp_path):
    c = db.connect(tmp_path / "index.db")
    stats = takeout.scan(c, takeout_dir, progress=lambda *_: None)
    analyze.analyze(c, workers=2, progress=lambda *_: None)
    grouping.build(c)
    return c, stats


@pytest.fixture
def conn(scanned):
    return scanned[0]


def by_name(conn):
    return {r["filename"]: r for r in conn.execute("SELECT * FROM items")}


def test_scan_merges_album_copies_and_edits(scanned):
    conn, stats = scanned
    items = by_name(conn)
    assert len(items) == 7  # burst x3, other, original, WhatsApp copy, IMG_0002(1)
    assert stats["album_copies"] == 1
    assert stats["edited"] == 1
    original = items["IMG_0001.jpg"]
    assert original["edited_path"].endswith("IMG_0001-edited.jpg")
    assert original["lat"] == 15.5
    albums = conn.execute(
        "SELECT a.title FROM album_items ai JOIN albums a ON a.id = ai.album_id WHERE ai.item_id = ?",
        (original["id"],),
    ).fetchall()
    assert [a["title"] for a in albums] == ["Goa trip"]
    assert items["IMG_0002(1).jpg"]["taken_at"] == 1_717_000_000 + 20_000


def test_rescan_is_a_no_op(conn, takeout_dir):
    again = takeout.scan(conn, takeout_dir, progress=lambda *_: None)
    assert again["new_items"] == 0 and again["files"] == 0


def groups(conn, kind):
    rows = conn.execute(
        "SELECT g.id, i.filename, gi.keep FROM groups g JOIN group_items gi ON gi.group_id = g.id"
        " JOIN items i ON i.id = gi.item_id WHERE g.kind = ? ORDER BY g.id, gi.rank",
        (kind,),
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["id"], []).append((r["filename"], r["keep"]))
    return list(out.values())


def test_exact_duplicates_keep_the_larger_copy(conn):
    assert groups(conn, "exact") == [[("IMG_0001.jpg", 1), ("IMG-WA0001.jpg", 0)]]


def test_burst_keeps_the_sharp_shot(conn):
    near = groups(conn, "near")
    assert len(near) == 1
    assert near[0][0] == ("burst_1.jpg", 1)
    assert dict(near[0])["burst_3.jpg"] == 0


def test_sharpness_ranks_blur(takeout_dir):
    year = takeout_dir / "Takeout" / "Google Photos" / "Photos from 2024"
    s = [analyze.analyze_file(str(year / f"burst_{i}.jpg")).sharpness for i in (1, 2, 3)]
    assert s[0] > s[1] > s[2]


def test_decisions_override_proposals(conn, tmp_path):
    wa = by_name(conn)["IMG-WA0001.jpg"]
    conn.execute("INSERT INTO decisions(item_id, keep) VALUES (?, 1)", (wa["id"],))
    names = [r["filename"] for r in export.trash_rows(conn)]
    assert "IMG-WA0001.jpg" not in names
    assert "burst_3.jpg" in names


def test_export_writes_lists(conn, tmp_path):
    events.build(conn, events.EventSettings(min_items=1))
    stats = export.export(conn, tmp_path / "cleanup")
    assert stats["to_trash"] >= 2
    trash = (tmp_path / "cleanup" / "trash.html").read_text()
    assert "photos.google.com/search/IMG-WA0001.jpg" in trash
    assert (tmp_path / "cleanup" / "albums.csv").exists()


def test_review_server(conn, tmp_path):
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db_path, tmp_path / ".thumbs"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        near = json.load(urllib.request.urlopen(f"{base}/api/groups?kind=near"))
        assert len(near) == 1 and len(near[0]["items"]) == 3
        item = near[0]["items"][-1]
        req = urllib.request.Request(
            f"{base}/api/decision", data=json.dumps({"item_id": item["id"], "keep": True}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        assert json.load(urllib.request.urlopen(req))["ok"]
        near = json.load(urllib.request.urlopen(f"{base}/api/groups?kind=near"))
        assert near[0]["items"][-1]["keep"] == 1
        thumb = urllib.request.urlopen(f"{base}/thumb/{item['id']}").read()
        assert thumb[:2] == b"\xff\xd8"
        assert b"Photos organizer" in urllib.request.urlopen(base + "/").read()
    finally:
        server.shutdown()
