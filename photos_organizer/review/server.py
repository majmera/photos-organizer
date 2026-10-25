"""Local review page: a small web server on 127.0.0.1 for checking the tool's proposals."""

from __future__ import annotations

import json
import sqlite3
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import db
from ..thumbs import thumbnail

PAGE = Path(__file__).with_name("index.html")


def group_payload(conn: sqlite3.Connection, kind: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT g.id AS group_id, gi.rank, gi.score, gi.keep AS proposed, d.keep AS decided,
               i.id, i.filename, i.taken_at, i.width, i.height, i.size, i.sharpness, i.exposure,
               i.faces, i.eyes_open, i.framing, i.lat
        FROM groups g
        JOIN group_items gi ON gi.group_id = g.id
        JOIN items i ON i.id = gi.item_id
        LEFT JOIN decisions d ON d.item_id = i.id
        WHERE g.kind = ?
        ORDER BY MIN(i.taken_at) OVER (PARTITION BY g.id), g.id, gi.rank
        """,
        (kind,),
    ).fetchall()
    groups: dict[int, dict] = {}
    for r in rows:
        g = groups.setdefault(r["group_id"], {"id": r["group_id"], "kind": kind, "items": []})
        item = dict(r)
        item["keep"] = r["proposed"] if r["decided"] is None else r["decided"]
        g["items"].append(item)
    return list(groups.values())


def events_payload(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for e in conn.execute("SELECT * FROM events ORDER BY start_at"):
        ids = [r["item_id"] for r in conn.execute(
            "SELECT ei.item_id FROM event_items ei JOIN items i ON i.id = ei.item_id"
            " WHERE ei.event_id = ? AND i.kind = 'photo' ORDER BY i.taken_at", (e["id"],))]
        count = conn.execute("SELECT COUNT(*) FROM event_items WHERE event_id = ?", (e["id"],)).fetchone()[0]
        step = max(1, len(ids) // 12)
        out.append({**dict(e), "count": count, "samples": ids[::step][:12]})
    return out


def summary(conn: sqlite3.Connection) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "items": one("SELECT COUNT(*) FROM items"),
        "photos": one("SELECT COUNT(*) FROM items WHERE kind = 'photo'"),
        "errors": one("SELECT COUNT(*) FROM items WHERE error IS NOT NULL"),
        "exact_groups": one("SELECT COUNT(*) FROM groups WHERE kind = 'exact'"),
        "near_groups": one("SELECT COUNT(*) FROM groups WHERE kind = 'near'"),
        "events": one("SELECT COUNT(*) FROM events"),
        "to_trash": one(
            "SELECT COUNT(DISTINCT gi.item_id) FROM group_items gi LEFT JOIN decisions d USING (item_id)"
            " WHERE COALESCE(d.keep, gi.keep) = 0"
        ),
    }


def make_handler(db_path: Path, thumb_dir: Path):
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the terminal quiet
            pass

        def send(self, body: bytes, ctype: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def json(self, data) -> None:
            self.send(json.dumps(data).encode(), "application/json")

        def do_GET(self):
            url = urlparse(self.path)
            parts = url.path.strip("/").split("/")
            conn = db.connect(db_path)
            try:
                if url.path == "/":
                    self.send(PAGE.read_bytes(), "text/html; charset=utf-8")
                elif url.path == "/api/summary":
                    self.json(summary(conn))
                elif url.path == "/api/groups":
                    self.json(group_payload(conn, parse_qs(url.query).get("kind", ["near"])[0]))
                elif url.path == "/api/events":
                    self.json(events_payload(conn))
                elif parts[0] in ("thumb", "full") and len(parts) == 2 and parts[1].isdigit():
                    row = conn.execute("SELECT path FROM items WHERE id = ?", (int(parts[1]),)).fetchone()
                    if not row:
                        return self.send(b"not found", "text/plain", HTTPStatus.NOT_FOUND)
                    side = 360 if parts[0] == "thumb" else 1600
                    self.send(thumbnail(row["path"], int(parts[1]), side, thumb_dir).read_bytes(), "image/jpeg")
                else:
                    self.send(b"not found", "text/plain", HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self.send(str(exc).encode(), "text/plain", HTTPStatus.INTERNAL_SERVER_ERROR)
            finally:
                conn.close()

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            with lock:
                conn = db.connect(db_path)
                try:
                    if self.path == "/api/decision":
                        conn.execute(
                            "INSERT INTO decisions(item_id, keep) VALUES (?, ?)"
                            " ON CONFLICT(item_id) DO UPDATE SET keep = excluded.keep",
                            (int(body["item_id"]), int(bool(body["keep"]))),
                        )
                    elif self.path == "/api/event":
                        conn.execute(
                            "UPDATE events SET accepted = COALESCE(?, accepted), title = COALESCE(?, title)"
                            " WHERE id = ?",
                            (body.get("accepted"), body.get("title"), int(body["id"])),
                        )
                    else:
                        return self.send(b"not found", "text/plain", HTTPStatus.NOT_FOUND)
                    conn.commit()
                    self.json({"ok": True})
                finally:
                    conn.close()

    return Handler


def serve(db_path: Path, port: int = 8765, open_browser: bool = True) -> None:
    thumb_dir = db_path.parent / ".thumbs"
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(db_path, thumb_dir))
    url = f"http://127.0.0.1:{port}/"
    print(f"Review page at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
