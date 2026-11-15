"""Company job boards: poll (every few minutes) and discover (daily).

Poll: every active board in parallel (1 request each) -> job ids not seen on that board before -> Stage 1
(title/company + location) -> same-role dedupe (also across sources) -> description (in the list, or one detail
call) -> stored as a pending job -> scored and notified like every other job. Rejects are only counted.
The first successful poll of a board is a BASELINE: its current jobs are remembered, never alerted.

Discover: companies (seen in any source, or listed in config) not checked yet / due for a re-check -> probe each
provider with slugs guessed from the name (name-verified where the API returns one) -> add found boards.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx

from .config import Config
from .db import Store, utcnow
from .filters import check_ats_location, check_company_title
from .models import JobDetail
from .sources import boards as ats
from .sources.boards import AtsError, Board, RateLimited

log = logging.getLogger("jobpoller")


def _board(row: dict) -> Board:
    return Board.from_dict({k: v for k, v in row.items() if k != "added_at"} | {"added_at": None})


def _fetch_all(boards: list[Board], workers: int, known: dict[str, set | None],
               shared: httpx.Client) -> dict[str, tuple[list | None, Exception | None]]:
    """Fetch every board in parallel over one shared client. Never raises. Descriptions of jobs we already know
    (or of a baseline poll) are dropped right away to keep memory low."""

    def one(b: Board):
        try:
            jobs = ats.fetch_board(shared, b)
        except (AtsError, httpx.HTTPError, ValueError) as e:
            return b.id, (None, e)
        k = known.get(b.id)
        for j in jobs:
            if k is None or j.id in k:
                j.description = ""
        return b.id, (jobs, None)

    with ThreadPoolExecutor(max(1, workers)) as ex:
        return dict(ex.map(one, boards))


def poll_boards(cfg: Config, db: Store) -> dict:
    a = cfg.job_boards
    out = {"boards": 0, "ok": 0, "failed": 0, "baseline": 0, "new": 0, "passed": 0, "duplicate": 0,
           "rejected": 0, "rate_limited": []}
    boards = [_board(r) for r in db.list_boards("active") if r["ats"] in a.providers]
    out["boards"] = len(boards)
    if not boards:
        return out
    states = db.board_states()
    t0 = time.monotonic()
    now = utcnow()
    limited: set[str] = set()
    with ats.client(a.timeout_seconds, pool=a.workers) as c:
        known = {b.id: (set(states[b.id]["known_ids"]) if states.get(b.id, {}).get("known_ids") is not None
                        else None) for b in boards}
        fetched = _fetch_all(boards, a.workers, known, c)
        log.info("boards: fetched %d in %.1fs", len(boards), time.monotonic() - t0)
        for b in boards:
            st = states.get(b.id) or {"failures": 0}
            jobs, err = fetched[b.id]
            if err is not None:
                if isinstance(err, RateLimited):
                    limited.add(b.ats)
                failures = int(st.get("failures") or 0) + 1
                dead = st.get("dead_at") or (now if failures >= a.dead_after_failures else None)
                db.save_board_state(b.id, failures=failures, last_error=f"{type(err).__name__}: {err}"[:200],
                                    dead_at=dead, known_ids=st.get("known_ids"))
                if dead and not st.get("dead_at"):
                    db.set_board_status(b.id, "dead", f"failing since {dead.isoformat(timespec='seconds')}")
                    log.warning("boards: %s failing for %d polls - marked dead (%s)", b.id, failures, err)
                out["failed"] += 1
                continue
            ids = [j.id for j in jobs]
            if known[b.id] is None:   # first successful poll: baseline, no alerts
                db.save_board_state(b.id, known_ids=sorted(ids), jobs=len(ids), failures=0, last_error=None,
                                    first_polled_at=now, last_ok_at=now, dead_at=None)
                out["baseline"] += 1
                out["ok"] += 1
                log.info("boards: %s baseline - %d open jobs remembered (not alerted)", b.id, len(ids))
                continue
            retry: set[str] = set()   # new passes whose description couldn't be fetched: try next poll
            for j in jobs:
                if j.id in known[b.id]:
                    continue
                res = _process(cfg, db, c, b, j)
                if res == "retry":
                    retry.add(j.id)
                else:
                    out[res] += 1
                    if res != "rejected":
                        out["new"] += 1
            db.save_board_state(b.id, known_ids=sorted(set(ids) - retry), jobs=len(ids), failures=0,
                                last_error=None, first_polled_at=st.get("first_polled_at"), last_ok_at=now,
                                dead_at=None)
            out["ok"] += 1
    out["rate_limited"] = sorted(limited)
    for p in limited:
        _warn_rate_limited(db, p)
    log.info("boards poll: %s", out)
    return out


def _process(cfg: Config, db: Store, c: httpx.Client, b: Board, j: ats.AtsJob) -> str:
    job = ats.to_job(b, j)
    if check_company_title(job, cfg.filters) or check_ats_location(job, cfg.filters, j.remote):
        return "rejected"
    if db.is_seen(job.key):
        return "duplicate"
    db.touch_company(job)
    dup = db.same_role(job.company_key, job.title)
    if dup:
        db.insert_job(job, status="duplicate", reason=f"duplicate_of:{dup}", search_label=f"board {b.id}")
        return "duplicate"
    try:
        desc = ats.description(c, b, j)
    except RateLimited:
        return "retry"
    except (AtsError, httpx.HTTPError, ValueError) as e:
        log.warning("boards: %s job %s description failed (%s) - retry next poll", b.id, j.id, e)
        return "retry"
    db.insert_job(job, status="passed", reason=None, search_label=f"board {b.id}",
                  detail=JobDetail(description=desc or f"{job.title} at {job.company} ({job.location})"))
    log.info("boards: stage 1 pass (queued for scoring): %s · %s · %s [%s]", job.title, job.company,
             job.location, b.ats)
    return "passed"


def _warn_rate_limited(db: Store, provider: str) -> None:
    """At most one warning per provider per day."""
    day = utcnow().date().isoformat()
    key = f"boards:{provider}"
    if db.source_state(key).get("warned_429") == day:
        return
    db.update_source_state(key, warned_429=day)
    from .notify import send_warning
    send_warning(f"⚠️ {provider}: HTTP 429 (rate limited) while polling job boards - skipped, retrying next poll.")


# ---- discovery ------------------------------------------------------------------------------------------------
def _to_probe(entry: dict | None, a) -> list[str]:
    """Providers to probe for this company now: all when new or due for a re-check, else only providers it was
    never successfully checked against (e.g. a provider added in a later version)."""
    if not entry:
        return list(a.providers)
    if entry["status"] in ("none", "error") and utcnow() - entry["checked_at"] >= timedelta(
            days=a.discover_recheck_none_days if entry["status"] == "none" else 1):
        return list(a.providers)
    done = set(entry.get("providers") or [])
    return [p for p in a.providers if p not in done]


def discover(cfg: Config, db: Store) -> dict:
    """Guess boards from company names. Companies you listed in config go first, then the ones with the most
    Stage 1 passes. Boards found are added; boards with > LARGE_BOARD jobs are added paused (aggregators)."""
    a = cfg.job_boards
    disc = db.discovery()
    rows = db.list_boards()
    have = {r["id"] for r in rows}
    have_company = {company_key(r["company"]) for r in rows if r["status"] != "dead"}
    todo = []
    for co in db.companies_for_discovery():
        entry = disc.get(co["company_key"])
        providers = _to_probe(entry, a)
        if company_key(co["name"]) in have_company and entry is None:
            continue   # board added by hand / seed / careers page: leave it alone
        if providers:
            todo.append((co, providers))
    todo.sort(key=lambda t: (t[0]["added_by"] != "config", -int(t[0]["jobs_passed"] or 0)))
    todo = todo[:a.discover_max_companies]
    out = {"due": len(todo), "checked": 0, "found": 0, "added": [], "rate_limited": []}
    limited: set[str] = set()
    shared = ats.client(a.timeout_seconds, pool=a.workers)

    def probe(item):
        co, providers = item
        found, errors, ok = [], 0, []
        for p in providers:
            if p in limited:
                errors += 1
                continue
            try:
                f = ats.probe_company(shared, p, co["name"])
            except RateLimited:
                limited.add(p)
                errors += 1
                continue
            ok.append(p)   # only providers that really answered count as checked
            if f:
                found.append(f)
        return co, found, errors, ok

    new_boards: list[dict] = []
    try:
        with ThreadPoolExecutor(max(1, a.workers)) as ex:
            for co, found, errors, ok in ex.map(probe, todo):
                out["checked"] += 1
                prev = disc.get(co["company_key"]) or {}
                checked = sorted(set(prev.get("providers") or []) | set(ok))
                ids = list(dict.fromkeys(list(prev.get("boards") or []) + [f.board.id for f in found]))
                for f in found:
                    b = f.board
                    d = b.to_dict() | {"added_by": "discovery"}
                    if f.jobs > ats.LARGE_BOARD:
                        d |= {"status": "paused", "note": f"large board ({f.jobs} jobs, aggregator?) - review"}
                    if b.id not in have:
                        new_boards.append(d)
                        have.add(b.id)
                out["found"] += 1 if found else 0
                db.save_discovery(co["company_key"], co["name"],
                                  "found" if ids else ("error" if errors else "none"), checked, ids)
    finally:
        shared.close()
    if new_boards:
        db.add_boards(new_boards)
        out["added"] = [f"{d['id']} ({d['company']}){' PAUSED' if d.get('status') == 'paused' else ''}"
                        for d in new_boards]
    out["rate_limited"] = sorted(limited)
    log.info("discovery: due=%d checked=%d found=%d added=%s", out["due"], out["checked"], out["found"],
             out["added"])
    return out


def company_key(name: str) -> str:
    return "".join(w for w in ats._ascii_words(name) if w not in ats._DROP)
