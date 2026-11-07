"""Write the manual cleanup lists: what to trash and which albums to create in Google Photos."""

from __future__ import annotations

import csv
import html
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .thumbs import thumbnail

SEARCH_URL = "https://photos.google.com/search/{}"


def search_link(filename: str) -> str:
    return SEARCH_URL.format(quote(filename))


def day(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %d %b %Y") if ts else "No date"


def stamp(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def trash_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Items the user (or, failing a decision, the tool) marked for trash, with why."""
    return conn.execute(
        """
        SELECT i.id, i.filename, i.taken_at, i.path, g.kind, gi.score,
               (SELECT k.filename FROM group_items kg JOIN items k ON k.id = kg.item_id
                 LEFT JOIN decisions kd ON kd.item_id = k.id
                 WHERE kg.group_id = g.id AND COALESCE(kd.keep, kg.keep) = 1
                 ORDER BY kg.rank LIMIT 1) AS kept
        FROM group_items gi
        JOIN groups g ON g.id = gi.group_id
        JOIN items i ON i.id = gi.item_id
        LEFT JOIN decisions d ON d.item_id = i.id
        WHERE COALESCE(d.keep, gi.keep) = 0
        GROUP BY i.id
        ORDER BY i.taken_at
        """
    ).fetchall()


def reason(r: sqlite3.Row) -> str:
    kept = f" of {r['kept']}" if r["kept"] else ""
    if r["kind"] == "exact":
        return f"Duplicate{kept}"
    return f"Weaker similar shot (score {r['score']:.2f}); keeping {r['kept'] or 'none'}"


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ --bg:#fff; --text:#1d1d1b; --muted:#6b6b66; --line:#e3e3de; --accent:#2f5fd0; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#161615; --text:#ececea; --muted:#a2a29c; --line:#34342f; --accent:#8aa8ff; }} }}
body {{ margin:0 auto; max-width:900px; padding:16px; background:var(--bg); color:var(--text); font:15px/1.45 -apple-system, system-ui, sans-serif; }}
h1 {{ font-size:22px; }} h2 {{ font-size:16px; margin:28px 0 8px; border-bottom:1px solid var(--line); padding-bottom:4px; }}
p {{ color:var(--muted); }} a {{ color:var(--accent); }}
.row {{ display:flex; gap:12px; align-items:center; padding:6px 0; border-bottom:1px solid var(--line); }}
.row img {{ width:96px; height:72px; object-fit:cover; border-radius:4px; flex:none; }}
.row.done {{ opacity:.4; }} .grid {{ display:flex; flex-wrap:wrap; gap:6px; }}
.grid img {{ width:96px; height:72px; object-fit:cover; border-radius:4px; }}
small {{ color:var(--muted); }}
</style></head><body>
<h1>{title}</h1>
{intro}
{body}
<script>
// Ticks are remembered in this browser so you can work through the list over several sittings.
document.querySelectorAll("input[type=checkbox][data-key]").forEach(box => {{
  const key = "po:" + box.dataset.key;
  try {{ box.checked = localStorage.getItem(key) === "1"; }} catch (e) {{}}
  box.closest(".row").classList.toggle("done", box.checked);
  box.addEventListener("change", () => {{
    try {{ localStorage.setItem(key, box.checked ? "1" : "0"); }} catch (e) {{}}
    box.closest(".row").classList.toggle("done", box.checked);
  }});
}});
</script>
</body></html>
"""


def write_trash(conn: sqlite3.Connection, out: Path, thumbs: Path) -> int:
    rows = trash_rows(conn)
    with open(out / "trash.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["filename", "taken", "reason", "google_photos_search", "local_path"])
        for r in rows:
            w.writerow([r["filename"], stamp(r["taken_at"]), reason(r), search_link(r["filename"]), r["path"]])

    parts, current = [], None
    for r in rows:
        if day(r["taken_at"]) != current:
            current = day(r["taken_at"])
            parts.append(f"<h2>{html.escape(current)}</h2>")
        thumb = thumbnail(r["path"], r["id"], 360, thumbs).relative_to(out)
        parts.append(
            f'<div class="row"><input type="checkbox" data-key="trash-{r["id"]}">'
            f'<img loading="lazy" src="{quote(str(thumb))}" alt="">'
            f'<div><a href="{search_link(r["filename"])}" target="_blank" rel="noopener">{html.escape(r["filename"])}</a>'
            f'<br><small>{stamp(r["taken_at"])} · {html.escape(reason(r))}</small></div></div>'
        )
    intro = (
        f"<p>{len(rows)} photos to move to the trash, oldest first. Click a filename to find it in Google Photos, "
        "check it is the right one, delete it there, then tick it here. Deleted items stay in the Google Photos "
        "trash for 60 days.</p>"
    )
    (out / "trash.html").write_text(
        PAGE.format(title="Photos to trash", intro=intro, body="\n".join(parts) or "<p>Nothing to trash.</p>"),
        encoding="utf-8",
    )
    return len(rows)


def write_albums(conn: sqlite3.Connection, out: Path, thumbs: Path) -> int:
    trashed = {r["id"] for r in trash_rows(conn)}
    events = conn.execute("SELECT * FROM events WHERE accepted = 1 ORDER BY start_at").fetchall()
    parts = []
    with open(out / "albums.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["album", "kind", "from", "to", "filename", "taken", "google_photos_search"])
        for e in events:
            items = [
                it for it in conn.execute(
                    "SELECT i.* FROM event_items ei JOIN items i ON i.id = ei.item_id"
                    " WHERE ei.event_id = ? ORDER BY i.taken_at", (e["id"],))
                if it["id"] not in trashed
            ]
            for it in items:
                w.writerow([e["title"], e["kind"], stamp(e["start_at"]), stamp(e["end_at"]),
                            it["filename"], stamp(it["taken_at"]), search_link(it["filename"])])
            photos = [it for it in items if it["kind"] == "photo"]
            step = max(1, len(photos) // 16)
            grid = "".join(
                f'<img loading="lazy" src="{quote(str(thumbnail(it["path"], it["id"], 360, thumbs).relative_to(out)))}" alt="">'
                for it in photos[::step][:16]
            )
            parts.append(
                f'<div class="row"><input type="checkbox" data-key="album-{e["id"]}">'
                f'<div><strong>{html.escape(e["title"])}</strong><br>'
                f'<small>{len(items)} items, {stamp(e["start_at"])} to {stamp(e["end_at"])}</small></div></div>'
                f'<div class="grid">{grid}</div>'
            )
    intro = (
        "<p>In Google Photos, create a new album with this name, choose <em>Add photos</em>, scroll to the first "
        "date and select through to the last one. Every photo in the date range belongs to the album, except the "
        "ones on the trash list. albums.csv lists each photo if you need to check.</p>"
    )
    (out / "albums.html").write_text(
        PAGE.format(title="Albums to create", intro=intro, body="\n".join(parts) or "<p>No albums suggested.</p>"),
        encoding="utf-8",
    )
    return len(events)


def export(conn: sqlite3.Connection, out: Path) -> dict[str, int]:
    out.mkdir(parents=True, exist_ok=True)
    thumbs = out / "thumbs"
    return {"to_trash": write_trash(conn, out, thumbs), "albums": write_albums(conn, out, thumbs)}
