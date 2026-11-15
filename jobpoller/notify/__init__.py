"""Notification channels. Console is always on; Telegram when its two variables are set in .env.

One message per job, ever (table `notifications`); a repost (same company + title) of an already-notified job is
skipped. A failing channel never stops the poller.
"""
from __future__ import annotations

import html
import json
import logging

from .telegram import Telegram, format_match

log = logging.getLogger("jobpoller")
_tg: Telegram | None = None


def telegram() -> Telegram:
    global _tg
    _tg = _tg or Telegram()
    return _tg


def _as_list(xs) -> list[str]:
    if isinstance(xs, str):
        xs = json.loads(xs or "[]")
    return list(xs or [])


def console_line(job, score: int, matched, gaps, red_flags, one_liner: str) -> str:
    return (f"\n{score}/10 · {job['title']} · {job['company']} · [{job['source']}]\n"
            f"{job.get('location') or '?'}\n"
            f"✓ {', '.join(_as_list(matched)) or '-'}   ✗ {', '.join(_as_list(gaps)) or '-'}   "
            f"⚠ {', '.join(_as_list(red_flags)) or '-'}\n» {one_liner}\n{job['url']}\n")


def notify_match(db, job, score: int, matched, gaps, red_flags, one_liner: str, console: bool = True) -> bool:
    """Send one match to every channel unless already sent. True if it went out on any channel."""
    if db.notified(job["job_key"]):
        return False
    dup = db.notified_same_role(job["company_norm"], job["title"])
    if dup:
        log.info("notify: skip %s - same company + title already sent as %s", job["job_key"], dup)
        db.mark_notified(job["job_key"], "dup")
        return False
    channels = []
    if console:
        print(console_line(job, score, matched, gaps, red_flags, one_liner), flush=True)
        channels.append("console")
    tg = telegram()
    if tg.enabled and tg.send(format_match(job, score, matched, gaps, red_flags, one_liner)):
        channels.append("telegram")
    if channels:
        db.mark_notified(job["job_key"], "+".join(channels))
    return bool(channels)


def send_warning(text: str) -> None:
    """Operational warning (rate limits, a source failing). Logged; also sent to Telegram when enabled."""
    log.warning(text)
    try:
        tg = telegram()
        if tg.enabled:
            tg.send(html.escape(text))
    except Exception as e:  # noqa: BLE001
        log.warning("telegram warning not sent: %s", e)


def test_notify() -> list[str]:
    """Send a test message on every enabled channel. Returns one line per channel."""
    out = ["console: ok (this line)"]
    tg = telegram()
    if not tg.enabled:
        out.append("telegram: not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env)")
    else:
        ok, problem = tg.send_checked("✅ job-poller test message - Telegram is set up.")
        out.append("telegram: ok" if ok else f"telegram: FAILED - {problem}")
    return out
