"""job-poller command line.

  python -m jobpoller run            # the service (what the container runs)
  python -m jobpoller once           # one pass of every source + scoring, then exit
  python -m jobpoller test-notify    # send a test message on every enabled channel
  python -m jobpoller careers URL NAME   # which job board is behind a careers page?
  python -m jobpoller discover       # guess boards for known companies now
  python -m jobpoller matches [--min 7]
  python -m jobpoller stats
  python -m jobpoller refresh-seed [--providers lever ashby]   # rebuild seed/boards.json (slow, ~1-2 h)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import database_url, load_config


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    for noisy in ("httpx", "httpcore", "google_genai"):   # request URLs can contain secrets (Telegram token)
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="job-poller")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="the service loop")
    r.add_argument("--only", nargs="+", choices=["boards", "discovery", "linkedin"], help="run only these tasks")
    r.add_argument("--skip", nargs="+", choices=["boards", "discovery", "linkedin"], help="run all but these tasks")
    sub.add_parser("once")
    sub.add_parser("test-notify")
    sub.add_parser("discover")
    sub.add_parser("stats")
    rs = sub.add_parser("refresh-seed")
    rs.add_argument("--providers", nargs="*", default=None)
    m = sub.add_parser("matches")
    m.add_argument("--min", type=int, default=None)
    c = sub.add_parser("careers")
    c.add_argument("url")
    c.add_argument("name")
    args = p.parse_args(argv)
    _setup_logging()

    if args.cmd == "test-notify":
        from .notify import test_notify
        print("\n".join(test_notify()))
        return 0
    if args.cmd == "careers":
        from .sources.careers import check_careers_page
        r = check_careers_page(args.url, args.name)
        print(json.dumps({"verdict": r.verdict, "boards": [b.id for b in r.boards], "detected": r.detected,
                          "jsonld_jobs": r.jsonld_jobs, "pages": r.pages, "error": r.error}, indent=1))
        return 0

    if args.cmd == "refresh-seed":
        import os
        from pathlib import Path

        from .seed_refresh import refresh
        seed_dir = Path(os.environ.get("JOBPOLLER_SEED", "seed/boards.json")).parent
        print(json.dumps(refresh(seed_dir, args.providers), indent=1))
        return 0

    from .db import Store
    cfg = load_config()
    db = Store(database_url())
    try:
        if args.cmd == "run":
            from .app import run_forever
            run_forever(cfg, db, set(args.only or ()) or None, set(args.skip or ()) or None)
        elif args.cmd == "once":
            from .app import run_once
            print(json.dumps(run_once(cfg, db), indent=1, default=str))
        elif args.cmd == "discover":
            from .boards_runner import discover
            print(json.dumps(discover(cfg, db), indent=1, default=str))
        elif args.cmd == "matches":
            for r in db.matches(args.min if args.min is not None else cfg.scorer.alert_threshold):
                print(f"{r['score']}/10 · {r['title']} · {r['company']} · {r['location']} [{r['source']}]\n"
                      f"  {r['one_liner']}\n  {r['url']}")
        elif args.cmd == "stats":
            print(json.dumps(db.counts(), indent=1))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
