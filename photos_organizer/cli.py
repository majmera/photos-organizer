"""Command line: photos-organizer run TAKEOUT_DIR, then review, then export."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from . import analyze, db, events, export, grouping, takeout

DEFAULT_DB = "photos-organizer.db"


def report(title: str, stats: dict, started: float) -> None:
    detail = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in stats.items())
    print(f"{title} ({time.time() - started:.0f}s): {detail}")


def cmd_scan(conn, args) -> None:
    t = time.time()
    report("Scanned", takeout.scan(conn, Path(args.takeout).expanduser()), t)


def cmd_analyze(conn, args) -> None:
    t = time.time()
    report("Analyzed", analyze.analyze(conn, workers=args.workers), t)


def cmd_group(conn, args) -> None:
    t = time.time()
    s = grouping.Settings(near_window=args.window, near_phash=args.similarity, keep=args.keep)
    report("Grouped", grouping.build(conn, s), t)


def cmd_albums(conn, args) -> None:
    t = time.time()
    report("Suggested albums", events.build(conn, events.EventSettings(gap_hours=args.gap_hours, min_items=args.min_items)), t)


def cmd_run(conn, args) -> None:
    for step in (cmd_scan, cmd_analyze, cmd_group, cmd_albums):
        step(conn, args)
    print(f"\nNext: photos-organizer review --db {args.db}")


def cmd_review(conn, args) -> None:
    from .review.server import serve

    conn.close()
    serve(Path(args.db).resolve(), port=args.port, open_browser=not args.no_browser)


def cmd_export(conn, args) -> None:
    out = Path(args.out)
    stats = export.export(conn, out)
    print(f"Wrote {out / 'trash.html'} ({stats['to_trash']} photos) and {out / 'albums.html'} ({stats['albums']} albums)")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="photos-organizer", description=__doc__)
    p.add_argument("--db", default=DEFAULT_DB, help=f"index file (default {DEFAULT_DB})")
    sub = p.add_subparsers(dest="command", required=True)

    def with_scan(sp):
        sp.add_argument("takeout", help="folder holding the extracted Takeout export")

    def with_analyze(sp):
        sp.add_argument("--workers", type=int, default=None, help="parallel processes (default: all cores)")

    def with_group(sp):
        sp.add_argument("--window", type=float, default=60, help="seconds between shots of one scene (default 60)")
        sp.add_argument("--similarity", type=int, default=20, help="max image-hash distance for similar shots (default 20)")
        sp.add_argument("--keep", type=int, default=2, help="most shots kept per similar group (default 2)")

    def with_albums(sp):
        sp.add_argument("--gap-hours", type=float, default=6, help="pause that starts a new event (default 6)")
        sp.add_argument("--min-items", type=int, default=10, help="smallest event worth an album (default 10)")

    sp = sub.add_parser("run", help="scan, analyze, group and suggest albums in one go")
    for f in (with_scan, with_analyze, with_group, with_albums):
        f(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("scan", help="index a Takeout export")
    with_scan(sp)
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("analyze", help="hash and score photos")
    with_analyze(sp)
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("group", help="find duplicates and similar shots")
    with_group(sp)
    sp.set_defaults(func=cmd_group)

    sp = sub.add_parser("albums", help="suggest event and trip albums")
    with_albums(sp)
    sp.set_defaults(func=cmd_albums)

    sp = sub.add_parser("review", help="open the review page")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--no-browser", action="store_true")
    sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("export", help="write the trash list and album list")
    sp.add_argument("--out", default="cleanup", help="output folder (default cleanup)")
    sp.set_defaults(func=cmd_export)

    args = p.parse_args(argv)
    args.func(db.connect(args.db), args)


if __name__ == "__main__":
    main()
