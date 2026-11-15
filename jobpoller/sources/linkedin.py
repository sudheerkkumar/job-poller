"""LinkedIn public guest endpoints. No login, no cookies."""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from ..models import Job, JobDetail

log = logging.getLogger(__name__)

SOURCE = "linkedin"
def state_source() -> str:
    """Key for LinkedIn's backoff state, request counters and run records."""
    return SOURCE
SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
VIEW_URL = "https://www.linkedin.com/jobs/view/{id}"
PAGE_SIZE = 10

# Honest client name - this tool does not pretend to be a browser.
HEADERS = {
    "User-Agent": "job-poller/0.1 (self-hosted job search; LinkedIn guest pages, slow fixed pacing)",
    "Accept-Language": "en-US,en;q=0.9",
}
_AUTH_REDIRECT = ("authwall", "login", "checkpoint", "signup", "uas/")


STOP = threading.Event()  # set by the SIGTERM/SIGINT handler: abort waits immediately


class Stopped(Exception):
    """Shutdown requested while waiting between requests."""


class Blocked(Exception):
    """LinkedIn rate-limited us or redirected to a login wall. Stop the cycle and back off."""

    def __init__(self, status: int, reason: str, headers: dict[str, str] | None = None,
                 retry_after_seconds: int | None = None):
        super().__init__(f"{status} {reason}")
        self.status = status
        self.reason = reason
        self.headers = headers or {}
        self.retry_after_seconds = retry_after_seconds
        self.partial: SearchResult | None = None  # set by search(): results gathered before the block


# Headers worth keeping from a 429. Everything else is kept too, except cookies.
_SKIP_HEADERS = {"set-cookie"}


def parse_retry_after(value: str | None) -> int | None:
    """Retry-After is either delta-seconds ("120") or an HTTP date. Returns seconds from now, or None."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return max(0, int((dt - datetime.now(UTC)).total_seconds()))


@dataclass
class SearchResult:
    jobs: list[Job] = field(default_factory=list)
    pages: int = 0
    requests: int = 0
    truncated: str | None = None  # "max_pages" | "hourly_budget": last page was full, more results likely exist
    http_status: int | None = None
    warning: str | None = None
    error: str | None = None


def _text(node: Node | None, selector: str | None = None) -> str:
    if node is not None and selector:
        node = node.css_first(selector)
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.text(separator=" ", strip=True)).strip()


def _company_url(href: str | None) -> str | None:
    if not href:
        return None
    m = re.search(r"linkedin\.com/company/([^/?#]+)", href)
    return f"https://www.linkedin.com/company/{m.group(1)}" if m else None


def parse_cards(html: str) -> list[Job]:
    tree = HTMLParser(html)
    jobs: list[Job] = []
    for card in tree.css("[data-entity-urn]"):
        m = re.search(r"urn:li:jobPosting:(\d+)", card.attributes.get("data-entity-urn") or "")
        if not m:
            continue
        job_id = m.group(1)
        company_node = card.css_first("h4.base-search-card__subtitle")
        link = company_node.css_first("a") if company_node else None
        t = card.css_first("time")
        title = _text(card, "h3.base-search-card__title")
        if not title:
            continue
        jobs.append(
            Job(
                source=SOURCE,
                source_id=job_id,
                title=title,
                company=_text(company_node) or "(unknown)",
                location=_text(card, "span.job-search-card__location"),
                url=VIEW_URL.format(id=job_id),
                posted_at=t.attributes.get("datetime") if t else None,
                posted_text=_text(t) or None,
                company_url=_company_url(link.attributes.get("href") if link else None),
            )
        )
    return jobs


_CRITERIA_KEYS = {
    "seniority level": "seniority",
    "nivel de antigüedad": "seniority",
    "employment type": "employment_type",
    "tipo de empleo": "employment_type",
    "job function": "job_function",
    "función laboral": "job_function",
    "industries": "industries",
    "sectores": "industries",
}


def parse_detail(html: str) -> JobDetail:
    tree = HTMLParser(html)
    desc = tree.css_first("div.show-more-less-html__markup") or tree.css_first("div.description__text")
    description = desc.text(separator="\n", strip=True) if desc else ""
    fields: dict[str, str] = {}
    for li in tree.css("li.description__job-criteria-item"):
        key = _CRITERIA_KEYS.get(_text(li, "h3.description__job-criteria-subheader").lower())
        if key:
            fields[key] = _text(li, "span.description__job-criteria-text")
    cap = tree.css_first("figcaption.num-applicants__caption") or tree.css_first(".num-applicants__caption")
    applicants = _text(cap) or None
    return JobDetail(description=description, applicants=applicants, **fields)


class LinkedInClient:
    def __init__(self, jitter: tuple[float, float],
                 on_request: Callable[[str, int | None], None] | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.jitter = jitter
        self.requests = 0
        self.first_request_at: float | None = None
        self.on_request = on_request  # called (kind, http_status) after every request, for the hourly budget
        self.http = httpx.Client(headers=HEADERS, timeout=20.0, follow_redirects=False, transport=transport)

    def close(self) -> None:
        self.http.close()

    def elapsed(self) -> float:
        return 0.0 if self.first_request_at is None else time.monotonic() - self.first_request_at

    def _get(self, url: str, params: dict | None = None, kind: str = "search") -> httpx.Response:
        if self.requests:
            if STOP.wait(random.uniform(*self.jitter)):
                raise Stopped()
        else:
            self.first_request_at = time.monotonic()
        self.requests += 1
        try:
            r = self.http.get(url, params=params)
        except httpx.HTTPError:
            if self.on_request:
                self.on_request(kind, None)
            raise
        if self.on_request:
            self.on_request(kind, r.status_code)
        if r.status_code in (429, 999):
            headers = {k: v for k, v in r.headers.items() if k.lower() not in _SKIP_HEADERS}
            raise Blocked(r.status_code, "rate limited", headers=headers,
                          retry_after_seconds=parse_retry_after(r.headers.get("retry-after")))
        if r.is_redirect:
            loc = r.headers.get("location", "")
            if any(k in loc.lower() for k in _AUTH_REDIRECT):
                raise Blocked(r.status_code, f"redirect to {loc[:100]}")
        return r

    def search(self, keywords: str, location: str, window_seconds: int, max_pages: int,
               budget: int | None = None, extra_params: dict | None = None) -> SearchResult:
        """Paginate one search until an empty/short page, max_pages, or the request budget. Raises Blocked.

        We only know we saw ALL results when a page comes back short (<10) or empty. If we stop on a full
        page (page cap or budget), `truncated` is set: LinkedIn gives no total count, so more may exist."""
        res = SearchResult()
        if max_pages <= 0:
            max_pages = 1000  # "no page cap": stop only on a short/empty page, the budget, or a block
        limit = max_pages if budget is None else max(0, min(max_pages, budget))
        for page in range(limit):
            params = {
                "keywords": keywords,
                "location": location,
                "f_TPR": f"r{window_seconds}",
                "start": page * PAGE_SIZE,
                **(extra_params or {}),  # e.g. {"f_EA": "true"} = under 10 applicants
            }
            res.requests += 1
            try:
                r = self._get(SEARCH_URL, params, kind="search")
            except Blocked as e:
                e.partial = res  # cards from the pages fetched before the block are still usable
                raise
            except httpx.HTTPError as e:
                res.error = f"{type(e).__name__}: {e}"
                break
            res.http_status = r.status_code
            if r.status_code != 200:
                # LinkedIn returns 400 when paging past the end; on page 0 it's a real error
                if page == 0:
                    res.error = f"HTTP {r.status_code}"
                break
            res.pages += 1
            cards = parse_cards(r.text)
            if not cards:
                if page == 0 and "base-card" in r.text:
                    res.warning = "HTML has job cards but none parsed - selectors may have changed"
                break
            res.jobs.extend(cards)
            if len(cards) < PAGE_SIZE:
                break
        else:
            # loop ran out of allowed pages and every page was full
            if limit > 0:
                res.truncated = "max_pages" if limit == max_pages else "hourly_budget"
        return res

    def detail(self, job_id: str) -> JobDetail | None:
        """Full description + criteria. None if closed/unavailable. Raises Blocked."""
        try:
            r = self._get(DETAIL_URL.format(id=job_id), kind="detail")
        except httpx.HTTPError as e:
            log.warning("detail %s failed: %s", job_id, e)
            return None
        if r.status_code != 200:
            log.info("detail %s: HTTP %s", job_id, r.status_code)
            return None
        return parse_detail(r.text)
