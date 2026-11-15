"""Telegram channel. Enabled when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set (see docs/telegram.md).
If Telegram is not configured or unreachable nothing breaks: matches are still logged and in `job-poller matches`.
"""
from __future__ import annotations

import html
import json
import logging
import os
import time

import httpx

log = logging.getLogger("jobpoller")
API = "https://api.telegram.org/bot{token}/sendMessage"


class Telegram:
    def __init__(self, chat_id: str | None = None, token: str | None = None) -> None:
        self.token = (token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip().strip("\"'")
        self.chat_id = (chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
        self._last = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        return self.send_checked(text)[0]

    def send_checked(self, text: str) -> tuple[bool, str]:
        """Send; returns (ok, problem). `problem` is Telegram's own description (e.g. "Bad Request: chat not
        found" = wrong chat id or the user never sent /start) - never contains the token."""
        if not self.token:
            return False, "bot token not configured"
        if not self.chat_id:
            return False, "no chat id"
        problem = ""
        for attempt in (1, 2):  # one retry on network errors
            wait = 1.1 - (time.monotonic() - self._last)  # Telegram: ~1 message/s per chat
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = httpx.post(API.format(token=self.token), timeout=15, data={
                    "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                    "disable_web_page_preview": "true"})
                if r.status_code == 200:
                    return True, ""
                if r.status_code == 429:
                    problem = "Telegram rate limit"
                    time.sleep(min(10.0, float(r.json().get("parameters", {}).get("retry_after", 5))))
                    continue
                try:
                    problem = str(r.json().get("description") or f"HTTP {r.status_code}")[:200]
                except ValueError:
                    problem = f"HTTP {r.status_code}"
                log.warning("telegram: HTTP %s %s", r.status_code, problem)
                return False, problem
            except httpx.HTTPError as e:
                problem = f"network error ({type(e).__name__})"  # str(e) could contain the URL = token
                log.warning("telegram: %s (attempt %d)", type(e).__name__, attempt)
        return False, problem


def _list(xs) -> str:
    if isinstance(xs, str):
        xs = json.loads(xs or "[]")
    return html.escape(", ".join(xs)) if xs else "-"


def format_match(job, score: int, matched, gaps, red_flags, one_liner: str) -> str:
    e = lambda v: html.escape(str(v or "?"))
    appl = f"\n👥 {html.escape(job['applicants'])}" if job.get("applicants") else ""
    posted = f" · {e(job['posted_text'])}" if job.get("posted_text") else ""
    return (f"<b>{score}/10 · {e(job['title'])}</b>\n"
            f"{e(job['company'])} · {e(job['location'])}{posted} · [{e(job['source'])}]{appl}\n"
            f"✓ {_list(matched)}\n✗ {_list(gaps)}\n⚠ {_list(red_flags)}\n"
            f"» {html.escape(one_liner or '')}\n{job['url']}")
