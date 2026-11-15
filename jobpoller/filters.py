"""Stage 1 local rules. Each check returns a reject reason, or None if the job passes."""
from __future__ import annotations

import re

from .config import Filters
from .models import Job, JobDetail, company_norm, norm


def _has_term(text_n: str, term: str) -> bool:
    t = norm(term)
    return bool(t) and re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", text_n) is not None


def check_card(job: Job, f: Filters) -> str | None:
    reason = check_company_title(job, f)
    if reason:
        return reason
    if location_status(job, f) is None:
        return f"location_not_accepted:{job.location}"
    return None


def check_ats_location(job: Job, f: Filters, remote_flag: bool = False) -> str | None:
    """ATS boards list jobs worldwide: pass Spain locations, and remote jobs whose location names an accepted
    remote region (Europe/EMEA/EU...). Plain 'Remote' without a region is rejected (mostly US-only)."""
    loc_n = norm(job.location)
    if any(_has_term(loc_n, loc) for loc in f.locations_accepted_any):
        return None
    remote = remote_flag or has_remote_signal(job.location, f) or has_remote_signal(job.title, f)
    if remote and any(_has_term(loc_n, r) for r in f.remote_regions_any):
        return None
    return f"location_not_accepted:{job.location or '-'}{' (remote)' if remote else ''}"


def check_company_title(job: Job, f: Filters) -> str | None:
    if f.company_blocklist:
        c = job.company_key
        for b in f.company_blocklist:
            if company_norm(b) == c:
                return f"company_blocked:{b}"

    title_n = norm(job.title)
    for term in f.title_exclude_any:
        if _has_term(title_n, term):
            return f"title_excluded:{term}"
    if f.title_include_any and not any(_has_term(title_n, t) for t in f.title_include_any):
        return "title_no_include_match"
    return None


def location_status(job: Job, f: Filters) -> str | None:
    """'local' (Madrid area), 'remote' (title says remote), 'maybe_remote' (country-level, check description),
    or None (reject)."""
    loc_n = norm(job.location)
    if not f.locations_accepted_any or any(_has_term(loc_n, loc) for loc in f.locations_accepted_any):
        return "local"
    if has_remote_signal(job.title, f):
        return "remote"
    if any(loc_n == norm(r) for r in f.remote_candidate_locations):
        return "maybe_remote"
    return None


def has_remote_signal(text: str | None, f: Filters) -> bool:
    t = norm(text)
    return any(_has_term(t, k) for k in f.remote_keywords_any)


def check_detail(detail: JobDetail, f: Filters) -> str | None:
    if detail.seniority and detail.seniority.strip().lower() in {s.lower() for s in f.seniority_exclude}:
        return f"seniority_excluded:{detail.seniority}"
    if detail.employment_type and detail.employment_type.strip().lower() in {
        e.lower() for e in f.employment_type_exclude
    }:
        return f"employment_type_excluded:{detail.employment_type}"
    return None
