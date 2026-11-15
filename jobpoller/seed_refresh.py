"""Rebuild seed/boards.json: which public job boards have open jobs in which regions?

1. Board names: the Common Crawl URL index (a free public web archive with an index API for research use) lists
   archived URLs under each provider's job-board domain, e.g. jobs.lever.co/* -> board slugs. It holds a sample
   of each site, not a complete list, and the index server is sometimes slow (Greenhouse is large: it may time out).
2. Regions: each board's public API is called once; a board is tagged with every region (seed/regions.yaml)
   where it has at least one open job.

Run occasionally (e.g. quarterly):  docker compose run --rm poller python -m jobpoller refresh-seed
Read-only, paced: one request every `pace` seconds per provider, stops a provider after three HTTP 429s.
"""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

from .sources import boards as ats
from .sources.boards import Board

log = logging.getLogger("jobpoller")
INDEX = "https://index.commoncrawl.org/collinfo.json"
PATTERNS = {
    "greenhouse": [("boards.greenhouse.io/*", None), ("job-boards.greenhouse.io/*", None),
                   ("job-boards.eu.greenhouse.io/*", "eu")],
    "lever": [("jobs.lever.co/*", "api.lever.co"), ("jobs.eu.lever.co/*", "api.eu.lever.co")],
    "ashby": [("jobs.ashbyhq.com/*", None)],
}
JUNK = {"embed", "v1", "api", "static", "assets", "_next", "js", "css", "images", "img", "search", "login", "jobs",
        "job_board", "privacy", "terms", "404", "careers", "www", "favicon.ico", "robots.txt", "sitemap.xml"}


def load_regions(path: Path) -> dict[str, dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def regions_for(jobs: list[ats.AtsJob], regions: dict[str, dict]) -> list[str]:
    out = []
    for name, r in regions.items():
        terms = [t.lower() for t in r.get("locations", [])]
        for j in jobs:
            loc = f" {(j.location or '').lower()}"
            hit = any(t in loc for t in terms)
            if hit and (not r.get("remote") or j.remote or "remote" in loc):
                out.append(name)
                break
    return out


def _slug(url: str) -> str | None:
    p = urlparse(url)
    seg = p.path.strip("/").split("/")[0]
    if "embed/job_board" in p.path and "for=" in p.query:
        seg = p.query.split("for=")[1].split("&")[0]
    seg = seg.lower()
    if not seg or seg in JUNK or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,80}", seg):
        return None
    return seg


def _cdx(c: httpx.Client, api: str, params: dict) -> httpx.Response | None:
    for attempt in range(5):
        try:
            r = c.get(api, params=params, timeout=120)
        except httpx.HTTPError:
            r = None
        if r is not None and r.status_code in (200, 404):
            return r
        if r is not None and r.status_code == 429:
            return None
        time.sleep(10 * (attempt + 1))   # 502/503: the index server is often overloaded
    return None


def enumerate_boards(provider: str, crawls: int = 2) -> set[tuple[str, str | None]]:
    found: set[tuple[str, str | None]] = set()
    with httpx.Client(headers={"User-Agent": ats.USER_AGENT}) as c:
        apis = [x["cdx-api"] for x in c.get(INDEX, timeout=60).json()][:crawls]
        for api in apis:
            for pattern, host in PATTERNS[provider]:
                r = _cdx(c, api, {"url": pattern, "showNumPages": "true"})
                pages = int(r.json().get("pages", 0)) if r is not None and r.status_code == 200 else 0
                for page in range(pages):
                    r = _cdx(c, api, {"url": pattern, "output": "json", "fl": "url", "page": page})
                    if r is None or r.status_code != 200:
                        log.warning("refresh-seed: %s %s page %d failed - skipped", api.split("/")[-1], pattern, page)
                        continue
                    for line in r.text.splitlines():
                        try:
                            s = _slug(json.loads(line)["url"])
                        except (ValueError, KeyError):
                            continue
                        if s:
                            found.add((s, host))
                    time.sleep(1)
    log.info("refresh-seed: %s - %d boards in the index", provider, len(found))
    return found


def tag_boards(provider: str, slugs: set[tuple[str, str | None]], regions: dict[str, dict],
               pace: float = 0.3) -> list[dict]:
    out, limited = [], 0
    with ats.client(20.0, pool=1) as c:
        for slug, host in sorted(slugs):
            if limited >= 3:
                log.warning("refresh-seed: %s rate limited 3 times - stopping this provider", provider)
                break
            time.sleep(pace)
            b = Board(ats=provider, slug=slug, company=slug, host=host)
            try:
                jobs = ats.fetch_board(c, b)
            except ats.RateLimited:
                limited += 1
                time.sleep(30)
                continue
            except (ats.AtsError, httpx.HTTPError, ValueError):
                continue
            tags = regions_for(jobs, regions)
            if tags:
                out.append({"id": b.id, "ats": provider, "slug": slug, "company": slug, "host": host,
                            "regions": tags, "jobs_at_scan": len(jobs)})
    return out


def refresh(seed_dir: Path, providers: list[str] | None = None) -> dict:
    regions = load_regions(seed_dir / "regions.yaml")
    providers = providers or list(PATTERNS)
    with ThreadPoolExecutor(len(providers)) as ex:   # one paced client per provider
        found = dict(zip(providers, ex.map(enumerate_boards, providers), strict=True))
        tagged = list(ex.map(lambda p: tag_boards(p, found[p], regions), providers))
    boards = sorted((b for bs in tagged for b in bs), key=lambda b: b["id"])
    doc = {"generated": date.today().isoformat(),
           "method": "Common Crawl URL index -> each board's public API once -> regions with open jobs",
           "boards": boards}
    (seed_dir / "boards.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return {"index_boards": {p: len(s) for p, s in found.items()}, "tagged": len(boards)}
