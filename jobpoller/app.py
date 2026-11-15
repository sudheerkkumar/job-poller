"""Wiring: startup sync (seed boards, your companies/boards from config) and the scheduler loop.

One process, one thread for the schedule: each task runs when it is due, then the scorer handles whatever new
jobs passed the filters. A task that fails is logged and retried at its next slot; the loop never dies on it.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from . import boards_runner, linkedin_runner, scorer
from .config import Config
from .db import Store, utcnow
from .models import company_norm
from .sources import boards as ats
from .sources import linkedin
from .sources.careers import check_careers_page

log = logging.getLogger("jobpoller")
SEED = Path(os.environ.get("JOBPOLLER_SEED", "seed/boards.json"))   # /app/seed/boards.json in the image


# ---- startup sync ---------------------------------------------------------------------------------------------
def parse_board(spec: str) -> dict:
    """'greenhouse:stripe', 'lever:acme@eu', 'greenhouse:feverup@eu', 'personio:acme@com' -> board dict."""
    ats_name, _, rest = spec.partition(":")
    slug, _, host = rest.partition("@")
    ats_name = ats_name.strip().lower()
    if ats_name not in ats.ATS_NAMES or not slug:
        raise ValueError(f"bad board {spec!r}: use <provider>:<slug>[@eu], providers: {', '.join(ats.ATS_NAMES)}")
    if ats_name == "lever":
        host = "api.eu.lever.co" if host == "eu" else "api.lever.co"
    b = ats.Board(ats=ats_name, slug=slug.strip(), company=slug.strip(), host=host or None)
    return b.to_dict()


def sync_seed(cfg: Config, db: Store, path: Path = SEED) -> int:
    if not cfg.job_boards.seed or not path.exists():
        return 0
    wanted = set(cfg.regions)
    rows = [b for b in json.loads(path.read_text(encoding="utf-8"))["boards"] if wanted & set(b.get("regions", []))]
    boards = [ats.Board(ats=b["ats"], slug=b["slug"], company=b["company"], host=b.get("host")).to_dict()
              | {"added_by": "seed", "note": f"seed: {', '.join(b['regions'])}"} for b in rows]
    n = db.add_boards(boards)
    if n:
        log.info("seed: added %d boards for regions %s", n, sorted(wanted))
    return n


def sync_config(cfg: Config, db: Store) -> dict:
    """Your boards and companies from config.yaml. Companies with a careers URL get their board detected now;
    companies without one are queued for name-based discovery."""
    out = {"boards": 0, "companies": 0, "careers": []}
    if cfg.boards:
        out["boards"] = db.add_boards([parse_board(s) | {"added_by": "config"} for s in cfg.boards])
    have = {r["id"] for r in db.list_boards()}
    for co in cfg.companies:
        db.add_config_company(company_norm(co.name), co.name, co.careers_url)
        out["companies"] += 1
        if co.careers_url:
            res = check_careers_page(co.careers_url, co.name)
            new = [b.to_dict() | {"added_by": "careers", "note": f"from {co.careers_url}"}
                   for b in res.boards if b.id not in have]
            db.add_boards(new)
            have |= {b["id"] for b in new}
            out["careers"].append(f"{co.name}: {res.verdict} {[b.id for b in res.boards] or res.detected or ''}")
            log.info("careers page %s (%s): %s %s", co.name, co.careers_url, res.verdict,
                     [b.id for b in res.boards] or res.detected or res.error or "")
    return out


# ---- schedule -------------------------------------------------------------------------------------------------
@dataclass
class Task:
    name: str
    every: timedelta
    fn: object
    next_at: object = None


class Stop(Exception):
    pass


def _install_signals() -> None:
    def handler(signum, _frame):
        log.info("signal %s: stopping after the current step", signum)
        linkedin.STOP.set()
        raise Stop()
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


TASK_NAMES = ("boards", "discovery", "linkedin")
LINKEDIN_CONTAINER_HINT = "docker compose --profile linkedin up -d"


def score_and_notify(cfg: Config, db: Store) -> dict:
    """Score pending jobs and notify. Only one process scores at a time (poller and linkedin container share the
    database): if another one holds the lock, it will pick these jobs up - skip instead of scoring twice."""
    if not db.try_lock("scoring"):
        return {"skipped": "another process is scoring"}
    try:
        return scorer.score_pending(cfg.scorer, db, min_notify=cfg.notify.min_score, console=cfg.notify.console)
    finally:
        db.unlock("scoring")


def tasks(cfg: Config, db: Store, only: set[str] | None = None, skip: set[str] | None = None) -> list[Task]:
    """The scheduled tasks for this process. `only` / `skip` split them across containers:
    the poller container runs `--skip linkedin`, the opt-in linkedin container runs `--only linkedin`."""
    wanted = set(only or TASK_NAMES) - set(skip or ())
    if "linkedin" not in wanted and cfg.linkedin.enabled:
        log.info("linkedin.enabled is true - LinkedIn runs in its own container, not here (start it with: %s)",
                 LINKEDIN_CONTAINER_HINT)
    if "linkedin" in wanted and not cfg.linkedin.enabled and only:
        log.warning("this process should run LinkedIn, but linkedin.enabled is false in config.yaml - idle. "
                    "Set it to true (and add searches), then restart, or stop this container.")
    t = []
    if cfg.job_boards.enabled:
        t.append(Task("boards", timedelta(minutes=cfg.job_boards.interval_minutes),
                      lambda: boards_runner.poll_boards(cfg, db)))
        t.append(Task("discovery", timedelta(hours=cfg.job_boards.discover_interval_hours),
                      lambda: boards_runner.discover(cfg, db)))
    if cfg.linkedin.enabled:
        if not cfg.linkedin.searches:
            log.warning("linkedin.enabled is true but no searches are configured - LinkedIn skipped")
        else:
            log.warning("LinkedIn polling is ON (opt-in). Guest pages only, slow fixed pacing; LinkedIn's terms do "
                        "not allow automated access - you are responsible for using it.")
            t.append(Task("linkedin", timedelta(minutes=cfg.linkedin.interval_minutes),
                          lambda: linkedin_runner.run_cycle(cfg, db)))
    return [x for x in t if x.name in wanted]


def run_once(cfg: Config, db: Store) -> dict:
    out = {"seed": sync_seed(cfg, db), "config": sync_config(cfg, db)}
    for task in tasks(cfg, db):
        if task.name == "discovery":
            continue
        out[task.name] = task.fn()
    out["scoring"] = score_and_notify(cfg, db)
    return out


def run_forever(cfg: Config, db: Store, only: set[str] | None = None, skip: set[str] | None = None) -> None:
    _install_signals()
    try:
        ts = tasks(cfg, db, only, skip)
        if any(t.name == "boards" for t in ts):
            sync_seed(cfg, db)
            sync_config(cfg, db)
        now = utcnow()
        for t in ts:
            t.next_at = now + (timedelta(minutes=5) if t.name == "discovery" else timedelta())
        log.info("job-poller running: %s", ", ".join(f"{t.name} every {t.every}" for t in ts) or "nothing enabled")
        while True:
            now = utcnow()
            ran = False
            for t in ts:
                if now >= t.next_at:
                    t.next_at = now + t.every
                    try:
                        t.fn()
                    except Stop:
                        raise
                    except Exception:
                        log.exception("task %s failed - retrying at its next slot", t.name)
                    ran = True
            if ran:
                try:
                    score_and_notify(cfg, db)
                except Stop:
                    raise
                except Exception:
                    log.exception("scoring failed - jobs stay pending")
            time.sleep(max(1.0, min((t.next_at - utcnow()).total_seconds() for t in ts)) if ts else 60.0)
    except Stop:
        log.info("stopped")
