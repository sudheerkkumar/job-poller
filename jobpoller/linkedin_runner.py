"""One LinkedIn poll cycle: search → seen check → Stage 1 → detail → Stage 1 (detail) → store/print."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Config, Search
from .db import Store, iso, parse_iso, utcnow
from .filters import check_card, check_detail, has_remote_signal, location_status
from .models import Job, JobDetail
from .sources import linkedin
from .sources.linkedin import Blocked, LinkedInClient, Stopped

log = logging.getLogger("jobpoller")


def _window(db: Store, search: Search, cfg) -> int:
    last = db.last_success(search.key)
    if last is None:
        return cfg.initial_window_seconds
    elapsed = int((utcnow() - last).total_seconds()) + 300  # 5 min overlap
    return max(cfg.base_window_seconds, min(cfg.max_window_seconds, elapsed))


def _request_budget(db: Store, cfg) -> float:
    """Remaining LinkedIn requests (search + detail + canary) in the rolling 60 min. 0 in config = no cap."""
    if cfg.max_requests_per_hour <= 0:
        return float("inf")
    used = db.requests_since(linkedin.state_source(), utcnow() - timedelta(hours=1))
    return max(0, cfg.max_requests_per_hour - used)


def process_jobs(cfg: Config, db: Store, client: LinkedInClient, search_label: str, jobs: list[Job],
                 allow_detail) -> tuple[int, int]:
    """Seen check -> Stage 1 -> detail -> Stage 1 (detail) -> store. `allow_detail()` returns False when no
    detail request may be made now; such Stage 1 passes stay unseen so a later cycle fetches them.
    Returns (new, passed). Raises Blocked from detail requests."""
    li = cfg.linkedin
    new = passed = 0
    for job in jobs:
        db.touch_company(job)
        if db.is_seen(job.key):
            continue
        reason = check_card(job, cfg.filters)
        detail = None
        if reason is None:
            dup = db.same_role(job.company_key, job.title)
            if dup:  # repost of a job we already passed: no detail request, no LLM call, no 2nd notification
                new += 1
                db.insert_job(job, status="duplicate", reason=f"duplicate_of:{dup}", search_label=search_label)
                continue
        if reason is None and li.fetch_details:
            if not allow_detail():
                continue  # left unseen: picked up (with its detail) next time
            try:
                detail = client.detail(job.source_id)
            except Blocked as e:
                e.counts = (new, passed)  # this job stays unseen (no detail yet)
                raise
            if detail is not None:
                reason = check_detail(detail, cfg.filters)
                if (reason is None and location_status(job, cfg.filters) == "maybe_remote"
                        and not has_remote_signal(detail.description, cfg.filters)):
                    reason = f"not_accepted_location_and_no_remote_signal:{job.location}"
        new += 1
        if reason:
            db.insert_job(job, status="rejected", reason=reason, search_label=search_label, detail=detail)
            if cfg.output.print_rejected:
                log.info("reject %s · %s · %s", job.title, job.company, reason)
            continue
        passed += 1
        db.insert_job(job, status="passed", reason=None, search_label=search_label, detail=detail)
        if cfg.scorer.enabled and detail is not None:
            log.info("stage1 pass (queued for scoring): %s · %s · %s", job.title, job.company, job.location)
        else:
            _print_match(job, detail)  # no scorer / no description: show the Stage 1 pass as-is
    return new, passed


def _print_match(job: Job, d: JobDetail | None) -> None:
    meta = " · ".join(x for x in [job.location, job.posted_text, d and d.seniority, d and d.employment_type] if x)
    note = "" if d is not None else "   (no description - detail fetch failed)"
    log.info("MATCH  %s · %s · [linkedin]\n         %s\n         %s%s", job.title, job.company, meta, job.url, note)


def _search_keep_partial(cfg: Config, db: Store, client: LinkedInClient, label: str, *args, **kw):
    """client.search(); on a block, first keep the cards of the pages already fetched (Stage 1 rejects and reposts are
    stored; passes stay unseen because no detail request may be made now, so the next run fetches them), then
    re-raise (without this, pages fetched before a 429 would be lost)."""
    try:
        return client.search(*args, **kw)
    except Blocked as e:
        if e.partial is not None and e.partial.jobs:
            new, passed = process_jobs(cfg, db, client, label, e.partial.jobs, allow_detail=lambda: False)
            log.info("[%s] blocked after %d pages: kept %d cards (new=%d, passes left for the next run)",
                     label, e.partial.pages, len(e.partial.jobs), new)
        raise


def _register_block(db: Store, cfg, e: Blocked, client: LinkedInClient) -> None:
    st = db.source_state(linkedin.state_source())
    failures = st["consecutive_failures"] + 1
    schedule_min = cfg.backoff_minutes[min(failures - 1, len(cfg.backoff_minutes) - 1)]
    backoff_s = schedule_min * 60
    if e.retry_after_seconds is not None:
        backoff_s = max(backoff_s, e.retry_after_seconds)  # never retry earlier than LinkedIn asked
    until = utcnow() + timedelta(seconds=backoff_s)
    db.update_source_state(linkedin.state_source(), consecutive_failures=failures, backoff_until=iso(until))

    last_hour = db.requests_since(linkedin.state_source(), utcnow() - timedelta(hours=1))
    log.warning("LinkedIn BLOCKED (%s) after %d requests this cycle in %.0fs (%d requests in the last 60 min)",
                e, client.requests, client.elapsed(), last_hour)
    if e.headers:
        log.warning("  response headers: %s", json.dumps(e.headers, sort_keys=True))
    if e.retry_after_seconds is not None:
        log.warning("  LinkedIn says retry after %ds (Retry-After header: %r)",
                    e.retry_after_seconds, e.headers.get("retry-after"))
    else:
        log.warning("  no Retry-After header - using our own backoff schedule (%d min, failure #%d)",
                    schedule_min, failures)
    log.warning("  next LinkedIn request not before %s (in %.0f min)", iso(until), backoff_s / 60)

    from .notify import send_warning
    send_warning(f"⚠️ LinkedIn {e.status} after {client.requests} requests this "
                 f"cycle ({last_hour} in the last 60 min). Backing off {backoff_s / 60:.0f} min "
                 f"(failure #{failures}), next try after {_local_time(until, cfg)}.")
    db.record_run(source=linkedin.state_source(), started_at=iso(utcnow()), outcome="blocked",
                  http_status=e.status, error=e.reason, requests=client.requests,
                  elapsed_seconds=round(client.elapsed(), 1), retry_after_seconds=e.retry_after_seconds,
                  response_headers=json.dumps(e.headers, sort_keys=True) if e.headers else None)


def _local_time(t: datetime | None, li) -> str:
    """'10:45 (Madrid)' style, in the timezone of the catch-up setting."""
    if t is None:
        return "never"
    tz = li.daily_catchup.timezone
    return f"{t.astimezone(ZoneInfo(tz)).strftime('%H:%M')} ({tz.split('/')[-1].replace('_', ' ')})"


def run_cycle(cfg: Config, db: Store) -> dict:
    """One poll of every search. Returns totals (cards, new, passed)."""
    li = cfg.linkedin
    totals = {"cards": 0, "new": 0, "passed": 0}
    if not li.enabled:
        return totals
    st = db.source_state(linkedin.state_source())
    backoff_until = parse_iso(st["backoff_until"])
    if backoff_until and utcnow() < backoff_until:
        log.info("LinkedIn in backoff until %s - skipping cycle", iso(backoff_until))
        return totals
    cycle = st["cycles"] + 1
    db.update_source_state(linkedin.state_source(), cycles=cycle)

    budget = _request_budget(db, li)
    log.info("cycle %d: %s of %s LinkedIn requests left this hour", cycle,
             "unlimited" if budget == float("inf") else int(budget), li.max_requests_per_hour or "no cap")

    # rotate the starting search so a tight budget doesn't always starve the same (last) search
    searches = li.searches
    if searches:
        k = (cycle - 1) % len(searches)
        searches = searches[k:] + searches[:k]

    client = LinkedInClient(li.jitter_seconds, on_request=lambda kind, status: db.log_request(
        linkedin.state_source(), kind, status))
    details_left = li.max_details_per_cycle if li.max_details_per_cycle > 0 else float("inf")

    def allow_detail() -> bool:
        nonlocal details_left, budget
        if details_left <= 0 or budget <= 0:
            return False
        details_left -= 1
        budget -= 1
        return True

    try:
        for search in searches:
            started = utcnow()
            window = _window(db, search, li)
            if budget <= 0:
                # not marked as success: next poll widens the window so these hours aren't lost
                log.warning("[%s] skipped - hourly LinkedIn request budget (%d) used up", search.label,
                            li.max_requests_per_hour)
                db.record_run(source=linkedin.state_source(), search_label=search.label, started_at=iso(started),
                              finished_at=iso(utcnow()), window_seconds=window, pages=0, cards=0, new_jobs=0,
                              passed=0, outcome="skipped_budget", requests=0)
                continue
            res = _search_keep_partial(cfg, db, client, search.label, search.keywords, search.location, window,
                                       li.max_pages, budget=None if budget == float("inf") else int(budget))
            budget -= res.requests
            new, passed = process_jobs(cfg, db, client, search.label, res.jobs, allow_detail)

            if res.error:
                outcome = "error"
            elif res.truncated:
                outcome = "truncated"
            elif res.warning:
                outcome = "warning"
            else:
                outcome = "ok"
            if res.warning:
                log.warning("[%s] %s", search.label, res.warning)
            if res.truncated:
                log.warning("[%s] TRUNCATED (%s): stopped after %d full pages - more results likely exist "
                            "that we did not see", search.label, res.truncated, res.pages)
            if res.error:
                log.warning("[%s] search error: %s", search.label, res.error)
            else:
                # truncated still counts as success: re-polling the same window would mostly return the same
                # first pages again (results are not date-sorted), so widening would not recover the rest.
                db.mark_success(search.key, started)
            db.record_run(
                source=linkedin.state_source(), search_label=search.label, started_at=iso(started), finished_at=iso(utcnow()),
                window_seconds=window, pages=res.pages, cards=len(res.jobs), new_jobs=new, passed=passed,
                outcome=outcome, http_status=res.http_status, error=res.error or res.warning,
                requests=res.requests, truncated=res.truncated,
            )
            log.info("[%s] window=%ds pages=%d cards=%d new=%d passed=%d%s",
                     search.label, window, res.pages, len(res.jobs), new, passed,
                     f" TRUNCATED({res.truncated})" if res.truncated else "")
            totals["cards"] += len(res.jobs)
            totals["new"] += new
            totals["passed"] += passed

        if li.canary.every_n_cycles and (cycle - 1) % li.canary.every_n_cycles == 0:
            if budget > 0:
                _canary(client, li, db)
                budget -= 1
            else:
                log.info("canary skipped - hourly search budget used up")

        db.update_source_state(linkedin.state_source(), consecutive_failures=0, backoff_until=None)
    except Blocked as e:
        _register_block(db, li, e, client)
    except Stopped:
        # the interrupted search is not marked successful, so its next poll widens the window
        log.info("shutdown requested - cycle %d stopped early", cycle)
    finally:
        log.info("cycle %d done: cards=%d new=%d passed=%d requests=%d in %.0fs",
                 cycle, totals["cards"], totals["new"], totals["passed"], client.requests, client.elapsed())
        client.close()
    return totals


def _canary(client: LinkedInClient, li, db: Store) -> None:
    c = li.canary
    res = client.search(c.keywords, c.location, c.window_seconds, max_pages=1)
    ok = res.error is None and len(res.jobs) > 0
    db.record_run(source=linkedin.state_source(), search_label=f"canary: {c.keywords} @ {c.location}",
                  started_at=iso(utcnow()), window_seconds=c.window_seconds, pages=res.pages,
                  cards=len(res.jobs), outcome="ok" if ok else "warning", http_status=res.http_status,
                  error=res.error or res.warning, requests=res.requests)
    if ok:
        log.info("canary ok (%d cards)", len(res.jobs))
    else:
        log.warning("CANARY FAILED: broad search returned %d cards (%s) - LinkedIn HTML may have changed or we are "
                    "being soft-blocked", len(res.jobs), res.error or res.warning or "empty")


def catchup_due(cfg: Config, db: Store, at: str | None = None) -> str | None:
    """Local date string if today's daily catch-up hasn't run yet and it's past its start time (`at`, default the
    configured one), else None."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    c = cfg.linkedin.daily_catchup
    if not c.enabled:
        return None
    now = datetime.now(ZoneInfo(c.timezone))
    hh, mm = (int(x) for x in (at or c.at).split(":"))
    if (now.hour, now.minute) < (hh, mm):
        return None
    day = now.date().isoformat()
    return None if db.last_success(f"catchup|{day}") else day


def run_catchup(cfg: Config, db: Store, day: str) -> None:
    """Daily catch-up: every configured search over the last 24 h with extra params (default f_EA=true =
    under 10 applicants). Only jobs we haven't seen are processed, so it adds what the hourly polls missed."""
    li = cfg.linkedin
    c = li.daily_catchup
    st = db.source_state(linkedin.state_source())
    backoff_until = parse_iso(st["backoff_until"])
    if backoff_until and utcnow() < backoff_until:
        log.info("catch-up postponed: LinkedIn in backoff until %s", iso(backoff_until))
        return
    budget = _request_budget(db, li)
    client = LinkedInClient(li.jitter_seconds, on_request=lambda kind, status: db.log_request(
        linkedin.state_source(), kind, status))

    def allow_detail() -> bool:
        nonlocal budget
        if budget <= 0:
            return False
        budget -= 1
        return True

    total_new = total_passed = 0
    try:
        for search in li.searches:
            label = f"catchup {search.label} {c.extra_params}"
            started = utcnow()
            res = _search_keep_partial(cfg, db, client, label, search.keywords, search.location, c.window_seconds,
                                       li.max_pages, budget=None if budget == float("inf") else int(budget),
                                       extra_params=c.extra_params)
            budget -= res.requests
            new, passed = process_jobs(cfg, db, client, label, res.jobs, allow_detail)
            total_new += new
            total_passed += passed
            db.record_run(source=linkedin.state_source(), search_label=label, started_at=iso(started),
                          finished_at=iso(utcnow()), window_seconds=c.window_seconds, pages=res.pages,
                          cards=len(res.jobs), new_jobs=new, passed=passed,
                          outcome="error" if res.error else ("truncated" if res.truncated else "ok"),
                          http_status=res.http_status, error=res.error or res.warning, requests=res.requests,
                          truncated=res.truncated)
            log.info("[%s] pages=%d cards=%d new=%d passed=%d", label, res.pages, len(res.jobs), new, passed)
        db.mark_success(f"catchup|{day}", utcnow())
        log.info("catch-up %s done: new=%d passed=%d requests=%d", day, total_new, total_passed, client.requests)
    except Blocked as e:
        _register_block(db, li, e, client)  # not marked done: retried on the next loop after the backoff
    except Stopped:
        log.info("shutdown requested - catch-up stopped early")
    finally:
        client.close()
