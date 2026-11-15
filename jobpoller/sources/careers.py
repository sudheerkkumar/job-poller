"""Find the job board behind a company's careers page.

Many "own" careers sites are a branded page over a standard job-board provider: the page links to, embeds or
loads its jobs from Greenhouse, Lever, Ashby, Workday, SuccessFactors ... Fetch the careers page (following at
most two "careers / jobs" links), look for those providers, and when one we can read is found, verify it through
the provider's public API. That catches boards whose slug can't be guessed from the company name
(e.g. Aircall -> greenhouse "aircallioinc", Taxfix -> ashby "taxfix.com").

Respects robots.txt. One page request per hop, honest User-Agent, no JavaScript.
"""
from __future__ import annotations

import logging
import re
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.lexbor import LexborHTMLParser

from . import boards as ats
from .boards import Board

log = logging.getLogger("jobpoller")

# provider -> regex over the page HTML; groups = (slug, [host hint])
PROVIDERS: dict[str, str] = {
    "greenhouse": r"(?:boards|job-boards)(\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)"
                  r"|boards-api(\.eu)?\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)",
    "lever": r"jobs(\.eu)?\.lever\.co/([A-Za-z0-9_.-]+)",
    "ashby": r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)",
    "smartrecruiters": r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)",
    "personio": r"([a-z0-9-]+)\.jobs\.personio\.(de|com)",
    "teamtailor": r"([a-z0-9-]+)\.teamtailor\.com",
    "recruitee": r"([a-z0-9-]+)\.recruitee\.com",
    # detected and reported, no reader yet
    "workday": r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com",
    "successfactors": r"(career\d*\.successfactors\.(?:com|eu)|jobs\.sap\.com|\.sapsf\.(?:com|eu))",
    "icims": r"([a-z0-9-]+)\.icims\.com",
    "taleo": r"([a-z0-9-]+)\.taleo\.net",
    "workable": r"apply\.workable\.com/([A-Za-z0-9_-]+)",
    "bamboohr": r"([a-z0-9-]+)\.bamboohr\.com/(?:careers|jobs)",
    "talentclue": r"([a-z0-9-]+)\.talentclue\.com",
    "softgarden": r"([a-z0-9-]+)\.softgarden\.io",
    "pinpoint": r"([a-z0-9-]+)\.pinpointhq\.com",
    "breezy": r"([a-z0-9-]+)\.breezy\.hr",
}
READABLE = ("greenhouse", "lever", "ashby", "smartrecruiters", "personio", "teamtailor", "recruitee")
JUNK_SLUGS = {"embed", "js", "v1", "jobs", "api", "www", "careers", "app", "static", "assets", "company"}
CAREER_LINK = re.compile(r"career|jobs?\b|empleo|trabaja|join[- ]?us|carreras|vacan|karriere|stellen|"
                         r"work[- ]with[- ]us|open[- ]positions|ofertas|see all jobs|ver ofertas|search jobs", re.IGNORECASE)
BLOCK_MARK = re.compile(r"cf-chl|just a moment|captcha|access denied|incapsula|perimeterx|datadome", re.IGNORECASE)


@dataclass
class CareersResult:
    url: str
    pages: list[str] = field(default_factory=list)
    detected: dict[str, list[str]] = field(default_factory=dict)  # provider -> slugs seen
    boards: list[Board] = field(default_factory=list)              # verified, readable boards
    jsonld_jobs: int = 0                                            # schema.org JobPosting entries seen
    verdict: str = "unclear"   # board | detected-no-reader | jsonld | blocked | robots-disallow | error | unclear
    error: str | None = None


def _robots_ok(c: httpx.Client, url: str) -> bool:
    p = urlparse(url)
    try:
        r = c.get(f"{p.scheme}://{p.netloc}/robots.txt")
    except httpx.HTTPError:
        return True
    if r.status_code != 200:
        return True
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(r.text.splitlines())
    return rp.can_fetch(ats.USER_AGENT, url)


def detect_providers(html: str) -> dict[str, list[tuple[str, str | None]]]:
    """provider -> [(slug, host)] found in the page."""
    out: dict[str, list[tuple[str, str | None]]] = {}
    for name, rx in PROVIDERS.items():
        for m in re.finditer(rx, html):
            g = [x for x in m.groups()]
            host = None
            if name == "greenhouse":
                eu = g[0] or g[2]
                slug = g[1] or g[3]
                host = "eu" if eu else None
            elif name == "lever":
                host = "api.eu.lever.co" if g[0] else "api.lever.co"
                slug = g[1]
            elif name == "personio":
                slug, host = g[0], g[1]
            else:
                slug = next((x for x in g if x), m.group(0))
            if not slug or slug.lower() in JUNK_SLUGS:
                continue
            pair = (slug.lower() if name not in ("ashby",) else slug, host)
            if pair not in out.setdefault(name, []):
                out[name].append(pair)
    return out


def _jsonld_jobs(tree: LexborHTMLParser) -> int:
    import json
    n = 0
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            d = json.loads(node.text() or "")
        except ValueError:
            continue
        stack = [d]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack += x
            elif isinstance(x, dict):
                t = x.get("@type")
                if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                    n += 1
                stack += [v for v in x.values() if isinstance(v, (list, dict))]
    return n


def _verify(c: httpx.Client, ats_name: str, slug: str, host: str | None, company: str) -> Board | None:
    b = Board(ats=ats_name, slug=slug, company=company, host=host)
    try:
        jobs = ats.fetch_board(c, b)
    except (ats.AtsError, httpx.HTTPError, ValueError):
        if ats_name == "greenhouse" and host is None:   # EU-hosted boards are linked without ".eu" sometimes
            return _verify(c, ats_name, slug, "eu", company)
        return None
    return b if jobs is not None else None


def check_careers_page(url: str, company: str, max_hops: int = 2, timeout: float = 20.0) -> CareersResult:
    res = CareersResult(url=url)
    headers = {"User-Agent": ats.USER_AGENT, "Accept": "text/html,application/xhtml+xml",
               "Accept-Language": "en,es;q=0.8"}
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as c:
        if not _robots_ok(c, url):
            res.verdict = "robots-disallow"
            return res
        seen: set[str] = set()
        for _ in range(max_hops + 1):
            try:
                r = c.get(url)
            except httpx.HTTPError as e:
                res.verdict, res.error = "error", type(e).__name__
                return res
            res.pages.append(str(r.url))
            seen.add(str(r.url).rstrip("/"))
            if r.status_code in (403, 429, 503) or (len(r.text) < 20000 and BLOCK_MARK.search(r.text[:5000])):
                res.verdict = "blocked"
                return res
            if r.status_code != 200:
                res.verdict, res.error = "error", f"HTTP {r.status_code}"
                return res
            tree = LexborHTMLParser(r.text)
            for name, pairs in detect_providers(r.text).items():
                res.detected.setdefault(name, [])
                res.detected[name] += [s for s, _ in pairs if s not in res.detected[name]]
                if name in READABLE:
                    for slug, host in pairs:
                        if any(b.id == f"{name}:{slug}" for b in res.boards):
                            continue
                        b = _verify(c, name, slug, host, company)
                        if b:
                            res.boards.append(b)
            res.jsonld_jobs += _jsonld_jobs(tree)
            if res.boards or res.detected or res.jsonld_jobs:
                break
            nxt = None
            for a in tree.css("a[href]"):
                href, text = a.attributes.get("href") or "", a.text(strip=True)
                if CAREER_LINK.search(text) or CAREER_LINK.search(href):
                    full = urljoin(str(r.url), href)
                    if full.startswith("http") and full.rstrip("/") not in seen:
                        nxt = full
                        break
            if not nxt:
                break
            url = nxt
    if res.boards:
        res.verdict = "board"
    elif res.detected:
        res.verdict = "detected-no-reader"
    elif res.jsonld_jobs:
        res.verdict = "jsonld"
    return res
