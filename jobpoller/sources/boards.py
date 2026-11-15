"""Public ATS job-board APIs (no key, no login): Greenhouse, Lever, Ashby, SmartRecruiters, Personio,
Teamtailor, Recruitee.

Checked against the real APIs (field-by-field):
- Greenhouse  boards-api.greenhouse.io/v1/boards/{slug}[/jobs[/{id}]]  board has `name`; list has title, location,
              first_published, absolute_url, company_name but NO description (content=true = 8 MB for Elastic) ->
              1 detail call per Stage 1 pass.
- Lever       api.lever.co | api.eu.lever.co /v0/postings/{slug}?mode=json  list has descriptionPlain + lists,
              createdAt (ms), categories.location/allLocations, country, workplaceType. No company name.
- Ashby       api.ashbyhq.com/posting-api/job-board/{slug}  list has descriptionPlain, publishedAt, location,
              secondaryLocations, isRemote, workplaceType, address. No company name.
- SmartRecruiters api.smartrecruiters.com/v1/companies/{id}/postings?country=es  (country filter works: devoteam
              957 -> 80, all es); posting has company.name, location, releasedDate; detail has jobAd sections.
              Unknown company = 200 with totalFound 0.
- Personio    {slug}.jobs.personio.de|.com/xml  positions with office, subcompany, createdAt, jobDescriptions (HTML).
- Teamtailor  {slug}.teamtailor.com/jobs.rss  channel title = company name; items have guid, title, description
              (HTML), pubDate, link, remoteStatus (fully|hybrid|none...), tt:locations. Default page = 100 items and NOT
              newest first -> always page with per_page/offset (per_page=1000 returned only part of a feed).
              Unknown slug = 404.
- Recruitee   {slug}.recruitee.com/api/offers/  every offer in one call: company_name, title, location, country,
              remote, description + requirements (HTML), published_at, careers_url. Unknown slug = 404.
Workable dropped: its widget endpoint returned 200/0 jobs for every slug tried (no way to tell a real board).
"""
from __future__ import annotations

import html as htmlmod
import logging
import re
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..models import Job, company_norm

log = logging.getLogger("jobpoller")

ATS_NAMES = ("greenhouse", "lever", "ashby", "smartrecruiters", "personio", "teamtailor", "recruitee")
NAME_CHECKED = ("greenhouse", "smartrecruiters", "personio", "teamtailor")  # API returns a company name -> loose slugs
TT_PAGE = 500
# min seconds between discovery probes per provider (all threads): Recruitee answered 429 to 8 unpaced workers
# in testing; one request every ~0.3 s probed ~650 companies without a 429
PROBE_GAP = {"recruitee": 0.35}
_pace_lock = threading.Lock()
_pace_next: dict[str, float] = {}


def _pace(ats: str) -> None:
    gap = PROBE_GAP.get(ats)
    if not gap:
        return
    with _pace_lock:
        now = time.monotonic()
        at = max(now, _pace_next.get(ats, 0.0))
        _pace_next[ats] = at + gap
    if at > now:
        time.sleep(at - now)
USER_AGENT = "job-poller/0.1 (self-hosted job search; polls public job-board APIs)"
LARGE_BOARD = 1000  # aggregators (Jobgether: 4894 jobs) are added paused for review


class AtsError(Exception):
    def __init__(self, status: int | None, msg: str):
        super().__init__(msg)
        self.status = status


class RateLimited(AtsError):
    pass


@dataclass
class AtsJob:
    id: str
    title: str
    location: str
    url: str
    posted_at: str | None = None
    remote: bool = False
    description: str | None = None   # None = needs a detail call (Greenhouse, SmartRecruiters)


@dataclass
class Board:
    ats: str
    slug: str
    company: str                     # display name (the LinkedIn one when discovered -> same-role dedupe works)
    host: str | None = None          # lever: api.lever.co | api.eu.lever.co, personio: de | com
    status: str = "active"           # active | paused | dead
    added_by: str = "discovery"      # discovery | manual
    added_at: str | None = None
    note: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.ats}:{self.slug}"

    @property
    def url(self) -> str:
        return board_url(self.ats, self.slug, self.host)

    def to_dict(self) -> dict:
        d = {"id": self.id, "ats": self.ats, "slug": self.slug, "company": self.company, "host": self.host,
             "status": self.status, "added_by": self.added_by, "added_at": self.added_at, "url": self.url,
             "note": self.note}
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict) -> Board:
        return cls(ats=d["ats"], slug=d["slug"], company=d.get("company") or d["slug"], host=d.get("host"),
                   status=d.get("status", "active"), added_by=d.get("added_by", "manual"),
                   added_at=d.get("added_at"), note=d.get("note"))


def board_url(ats: str, slug: str, host: str | None = None) -> str:
    return {
        "greenhouse": f"https://job-boards.{'eu.' if host == 'eu' else ''}greenhouse.io/{slug}",
        "lever": f"https://jobs.{'eu.' if host == 'api.eu.lever.co' else ''}lever.co/{slug}",
        "ashby": f"https://jobs.ashbyhq.com/{slug}",
        "smartrecruiters": f"https://jobs.smartrecruiters.com/{slug}",
        "personio": f"https://{slug}.jobs.personio.{host or 'de'}",
        "teamtailor": f"https://{slug}.teamtailor.com/jobs",
        "recruitee": f"https://{slug}.recruitee.com",
    }[ats]


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    if "&lt;" in s and "<" not in s:  # Greenhouse `content` is HTML-escaped HTML
        s = htmlmod.unescape(s)
    for tag in ("</p>", "<br>", "<br/>", "<br />", "</li>", "</h1>", "</h2>", "</h3>", "</h4>", "</div>"):
        s = s.replace(tag, tag + "\n")
    text = HTMLParser(s).text(separator=" ")
    return re.sub(r"[ \t ]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def _iso_ms(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


def _get(c: httpx.Client, url: str, **kw) -> httpx.Response:
    r = c.get(url, **kw)
    if r.status_code == 429:
        raise RateLimited(429, f"429 from {url.split('/')[2]}")
    return r


# ---- list one board (one request; Lever EU/Personio .com via board.host) -------------------------------------
def fetch_board(c: httpx.Client, b: Board) -> list[AtsJob]:
    """All open jobs of a board. Raises AtsError (404 = board gone) / RateLimited."""
    fn = {"greenhouse": _gh_list, "lever": _lever_list, "ashby": _ashby_list,
          "smartrecruiters": _sr_list, "personio": _personio_list, "teamtailor": _tt_list,
          "recruitee": _recruitee_list}[b.ats]
    return fn(c, b)


def _ok(r: httpx.Response) -> httpx.Response:
    if r.status_code != 200:
        raise AtsError(r.status_code, f"HTTP {r.status_code}")
    return r


def gh_api(host: str | None) -> str:
    """Greenhouse boards API base: US (default) or EU-hosted boards (host "eu")."""
    return "https://boards-api.eu.greenhouse.io" if host == "eu" else "https://boards-api.greenhouse.io"


def _gh_list(c, b):
    r = _ok(_get(c, f"{gh_api(b.host)}/v1/boards/{b.slug}/jobs"))
    out = []
    for j in r.json().get("jobs", []):
        locs = [(j.get("location") or {}).get("name") or ""]
        out.append(AtsJob(id=str(j["id"]), title=j.get("title") or "", location="; ".join(x for x in locs if x),
                          url=j.get("absolute_url") or "", posted_at=j.get("first_published") or j.get("updated_at")))
    return out


def gh_detail(c: httpx.Client, b: Board, job_id: str) -> str:
    r = _ok(_get(c, f"{gh_api(b.host)}/v1/boards/{b.slug}/jobs/{job_id}"))
    return html_to_text(r.json().get("content"))


def _lever_text(j: dict) -> str:
    parts = [j.get("openingPlain") or "", j.get("descriptionBodyPlain") or j.get("descriptionPlain") or ""]
    for lst in j.get("lists") or []:
        parts.append(f"{lst.get('text', '')}:\n{html_to_text(lst.get('content'))}")
    parts.append(j.get("additionalPlain") or "")
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def _lever_list(c, b):
    host = b.host or "api.lever.co"
    r = _ok(_get(c, f"https://{host}/v0/postings/{b.slug}", params={"mode": "json"}))
    out = []
    for j in r.json():
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location") or ""]
        loc = "; ".join(x for x in locs if x)
        if j.get("country"):
            loc = f"{loc} ({j['country']})" if loc else j["country"]
        out.append(AtsJob(id=j["id"], title=j.get("text") or "", location=loc, url=j.get("hostedUrl") or "",
                          posted_at=_iso_ms(j.get("createdAt")), remote=(j.get("workplaceType") == "remote"),
                          description=_lever_text(j)))
    return out


def _ashby_list(c, b):
    r = _ok(_get(c, f"https://api.ashbyhq.com/posting-api/job-board/{b.slug}"))
    out = []
    for j in r.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        locs = [j.get("location") or ""] + [x.get("location") or "" for x in j.get("secondaryLocations") or []]
        if addr.get("addressCountry"):
            locs.append(addr["addressCountry"])
        out.append(AtsJob(id=j["id"], title=(j.get("title") or "").strip(), location="; ".join(dict.fromkeys(x for x in locs if x)),
                          url=j.get("jobUrl") or "", posted_at=j.get("publishedAt"),
                          remote=bool(j.get("isRemote")) or j.get("workplaceType") == "Remote",
                          description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml"))))
    return out


def _sr_list(c, b):
    out, offset = [], 0
    while True:  # country=es server-side: only Spain postings (remote-EU ones listed elsewhere are missed)
        r = _ok(_get(c, f"https://api.smartrecruiters.com/v1/companies/{b.slug}/postings",
                     params={"country": "es", "limit": 100, "offset": offset}))
        d = r.json()
        for p in d.get("content", []):
            loc = p.get("location") or {}
            where = ", ".join(x for x in [loc.get("city"), loc.get("region"), "Spain"] if x)
            out.append(AtsJob(id=str(p["id"]), title=p.get("name") or "", location=where,
                              url=f"https://jobs.smartrecruiters.com/{b.slug}/{p['id']}",
                              posted_at=p.get("releasedDate"), remote=bool(loc.get("remote"))))
        offset += 100
        if offset >= int(d.get("totalFound") or 0) or not d.get("content") or offset >= 1000:
            return out


def sr_detail(c: httpx.Client, b: Board, job_id: str) -> str:
    r = _ok(_get(c, f"https://api.smartrecruiters.com/v1/companies/{b.slug}/postings/{job_id}"))
    secs = ((r.json().get("jobAd") or {}).get("sections") or {})
    return "\n\n".join(html_to_text((secs.get(k) or {}).get("text"))
                       for k in ("jobDescription", "qualifications", "additionalInformation") if secs.get(k))


def _personio_list(c, b):
    r = _ok(_get(c, f"https://{b.slug}.jobs.personio.{b.host or 'de'}/xml"))
    if not r.text.lstrip().startswith("<?xml"):
        raise AtsError(r.status_code, "not a Personio XML feed")
    out = []
    for p in ET.fromstring(r.content).findall("position"):
        pid = p.findtext("id") or ""
        offices = [p.findtext("office") or ""] + [o.text or "" for o in p.findall("additionalOffices/office")]
        desc = "\n\n".join(f"{d.findtext('name') or ''}\n{html_to_text(d.findtext('value'))}"
                           for d in p.findall("jobDescriptions/jobDescription"))
        out.append(AtsJob(id=pid, title=p.findtext("name") or "", location="; ".join(x for x in offices if x),
                          url=f"https://{b.slug}.jobs.personio.{b.host or 'de'}/job/{pid}",
                          posted_at=p.findtext("createdAt"), description=desc))
    return out


_TT = "{https://teamtailor.com/locations}"


def _rfc822(s: str | None) -> str | None:
    try:
        return parsedate_to_datetime(s).astimezone(UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


def _tt_feed(c, slug: str, per_page: int, offset: int = 0) -> ET.Element:
    r = _ok(_get(c, f"https://{slug}.teamtailor.com/jobs.rss", params={"per_page": per_page, "offset": offset}))
    if "xml" not in r.headers.get("content-type", ""):
        raise AtsError(r.status_code, "not a Teamtailor RSS feed")
    return ET.fromstring(r.content)


def _tt_list(c, b):
    out, offset = [], 0
    while True:   # the feed is not newest-first: read all of it (one page for almost every board)
        items = _tt_feed(c, b.slug, TT_PAGE, offset).findall("channel/item")
        for it in items:
            locs = []
            for loc in it.iter(f"{_TT}location"):
                name, city, country = (loc.findtext(f"{_TT}{k}") or "" for k in ("name", "city", "country"))
                locs.append(", ".join(dict.fromkeys(x for x in (name, city, country) if x)))
            remote = (it.findtext("remoteStatus") or "").lower() == "fully"
            out.append(AtsJob(id=it.findtext("guid") or it.findtext("link") or "", title=(it.findtext("title") or "").strip(),
                              location="; ".join(x for x in locs if x), url=it.findtext("link") or "",
                              posted_at=_rfc822(it.findtext("pubDate")), remote=remote,
                              description=html_to_text(it.findtext("description"))))
        offset += TT_PAGE
        if len(items) < TT_PAGE or offset >= 5000:
            return out


def _utc_text(s: str | None) -> str | None:   # Recruitee "2026-10-06 12:42:12 UTC"
    if not s:
        return None
    return s.replace(" UTC", "+00:00").replace(" ", "T", 1)


def _recruitee_list(c, b):
    r = _ok(_get(c, f"https://{b.slug}.recruitee.com/api/offers/"))
    out = []
    for o in r.json().get("offers") or []:
        locs = [x for x in [o.get("location")] + [f"{x.get('city') or ''}, {x.get('country') or ''}".strip(", ")
                                                  for x in o.get("locations") or []] if x]
        loc = "; ".join(dict.fromkeys(locs)) or (o.get("country") or "")
        if o.get("country") and o["country"] not in loc:
            loc = f"{loc} ({o['country']})"
        desc = "\n\n".join(html_to_text(o.get(k)) for k in ("description", "requirements") if o.get(k))
        out.append(AtsJob(id=str(o["id"]), title=(o.get("title") or "").strip(), location=loc,
                          url=o.get("careers_url") or "", posted_at=_utc_text(o.get("published_at")),
                          remote=bool(o.get("remote")), description=desc))
    return out


def description(c: httpx.Client, b: Board, j: AtsJob) -> str:
    if j.description is not None:
        return j.description
    if b.ats == "greenhouse":
        return gh_detail(c, b, j.id)
    if b.ats == "smartrecruiters":
        return sr_detail(c, b, j.id)
    return ""


def to_job(b: Board, j: AtsJob) -> Job:
    return Job(source=b.ats, source_id=f"{b.slug}:{j.id}", title=j.title, company=b.company,
               location=j.location, url=j.url or b.url, posted_at=j.posted_at, posted_text=ago(j.posted_at))


def ago(posted_at: str | None, now: datetime | None = None) -> str | None:
    if not posted_at:
        return None
    try:
        t = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    m = ((now or datetime.now(UTC)) - t).total_seconds() / 60
    if m < 0:
        return "just now"
    if m < 90:
        return f"{m:.0f} min ago"
    if m < 48 * 60:
        return f"{m / 60:.0f} h ago"
    return f"{m / 1440:.0f} days ago"


# ---- discovery: slug guesses + name verification -------------------------------------------------------------
_DROP = {"spain", "espana", "group", "grupo", "sl", "slu", "sa", "inc", "ltd", "llc", "gmbh", "bv", "plc", "corp",
         "europe", "latam", "technology", "technologies", "solutions", "iberia", "the", "and", "company", "co",
         "hq", "global", "international"}


def _ascii_words(name: str) -> list[str]:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).split()


def slug_candidates(name: str) -> tuple[list[str], list[str]]:
    """(strict, loose): strict = whole name (joined / hyphenated, with and without filler words) - safe for ATS that
    don't return a company name (Lever, Ashby); loose adds the first word - only used where the API returns a
    company name we can check (Greenhouse, SmartRecruiters, Personio)."""
    words = _ascii_words(name)
    core = [w for w in words if w not in _DROP] or words
    strict: list[str] = []
    for ws in (core, words):
        for s in ("".join(ws), "-".join(ws)):
            if len(s) >= 3 and s not in strict:
                strict.append(s)
    loose = list(strict)
    if core and len(core[0]) >= 4 and core[0] not in loose:
        loose.append(core[0])
    return strict[:4], loose[:5]


def same_company(a: str, b: str) -> bool:
    """LinkedIn name vs the name an ATS returns ('Docker, Inc' ~ 'Docker'; 'Block Labs' !~ 'Block')."""
    def key(n):
        ws = [w for w in _ascii_words(company_norm(n)) if w not in _DROP]
        return "".join(ws)
    ka, kb = key(a), key(b)
    return bool(ka) and ka == kb  # exact after dropping legal/filler words; "starts with" matched Block Labs ~ Block


@dataclass
class Found:
    board: Board
    jobs: int
    spain_jobs: int = 0


def probe_company(c: httpx.Client, ats: str, name: str) -> Found | None:
    """Try this company's slugs on one ATS. Returns the verified board or None. Raises RateLimited."""
    strict, loose = slug_candidates(name)
    for slug in (loose if ats in NAME_CHECKED else strict):
        try:
            f = _probe_one(c, ats, slug, name)
        except RateLimited:
            raise
        except (httpx.HTTPError, ValueError, ET.ParseError, AtsError, KeyError, AttributeError):
            f = None
        if f:
            return f
    return None


def _probe_one(c, ats, slug, name) -> Found | None:
    b = Board(ats=ats, slug=slug, company=name)
    if ats == "greenhouse":  # list without descriptions: small even for 400+ jobs; US host, then EU host
        for host in (None, "eu"):
            r = _get(c, f"{gh_api(host)}/v1/boards/{slug}")
            if r.status_code != 200 or not same_company(name, r.json().get("name") or ""):
                continue
            r = _get(c, f"{gh_api(host)}/v1/boards/{slug}/jobs")
            if r.status_code == 200:
                b.host = host
                return Found(b, len(r.json().get("jobs") or []))
        return None
    if ats == "lever":  # never download the whole list here: it has every description (Jobgether ~4900 jobs ->
        # out of memory at 256 MB). 1 posting = exists; posting #LARGE_BOARD+1 = large board.
        for host in ("api.lever.co", "api.eu.lever.co"):
            r = _get(c, f"https://{host}/v0/postings/{slug}", params={"mode": "json", "limit": 1})
            if r.status_code == 200 and isinstance(r.json(), list) and r.json():
                b.host = host
                big = _get(c, f"https://{host}/v0/postings/{slug}",
                           params={"mode": "json", "limit": 1, "skip": LARGE_BOARD})
                large = big.status_code == 200 and isinstance(big.json(), list) and bool(big.json())
                return Found(b, LARGE_BOARD + 1 if large else 1)
        return None
    if ats == "ashby":
        r = _get(c, f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        if r.status_code != 200:
            return None
        n = len(r.json().get("jobs") or [])
        return Found(b, n) if n else None
    if ats == "smartrecruiters":
        r = _get(c, f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", params={"limit": 1})
        d = r.json() if r.status_code == 200 else {}
        if not d.get("totalFound") or not same_company(name, ((d.get("content") or [{}])[0].get("company") or {}).get("name") or ""):
            return None
        return Found(b, int(d["totalFound"]))
    if ats == "personio":
        for tld in ("de", "com"):
            r = _get(c, f"https://{slug}.jobs.personio.{tld}/xml")
            if r.status_code == 200 and r.text.lstrip().startswith("<?xml"):
                if "lorem ipsum" in r.text.lower():  # demo/test accounts
                    return None
                root = ET.fromstring(r.content)
                pos = root.findall("position")
                sub = {p.findtext("subcompany") or "" for p in pos} - {""}
                strict, _ = slug_candidates(name)
                if not pos or not (any(same_company(name, s) for s in sub) or (not sub and slug in strict)):
                    return None
                b.host = tld
                return Found(b, len(pos))
        return None
    if ats == "teamtailor":  # per_page=1: just the channel title (= company name); offset LARGE_BOARD = large?
        r = _get(c, f"https://{slug}.teamtailor.com/jobs.rss", params={"per_page": 1})
        if r.status_code != 200 or "xml" not in r.headers.get("content-type", ""):
            return None
        if not same_company(name, ET.fromstring(r.content).findtext("channel/title") or ""):
            return None
        big = _tt_feed(c, slug, 1, LARGE_BOARD).findall("channel/item")
        return Found(b, LARGE_BOARD + 1 if big else 1)
    if ats == "recruitee":   # strict slugs only; offers carry company_name -> must match when there are offers
        _pace(ats)
        r = _get(c, f"https://{slug}.recruitee.com/api/offers/")
        if r.status_code != 200:
            return None
        offers = r.json().get("offers") or []
        names = {o.get("company_name") or "" for o in offers} - {""}
        if not offers or (names and not any(same_company(name, n) for n in names)):
            return None
        return Found(b, len(offers))
    raise ValueError(ats)


def client(timeout: float = 20.0, pool: int = 8) -> httpx.Client:
    """One per run, shared by all threads (httpx.Client is thread-safe)."""
    return httpx.Client(timeout=timeout, follow_redirects=False, headers={"User-Agent": USER_AGENT},
                        limits=httpx.Limits(max_connections=pool * 2, max_keepalive_connections=pool))
