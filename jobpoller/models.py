from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel


def norm(text: str | None) -> str:
    """Lowercase, strip accents, collapse punctuation to single spaces (keeps + and #)."""
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9+#]+", " ", s.lower()).strip()


_LEGAL_SUFFIX = re.compile(
    r"\b(s ?l ?u?|s ?a ?u?|inc|ltd|llc|gmbh|bv|plc|corp|co|sociedad limitada|sociedad anonima)$"
)


def company_norm(name: str | None) -> str:
    n = norm(name)
    return _LEGAL_SUFFIX.sub("", n).strip() or n


class Job(BaseModel):
    """Normalized job returned by every source adapter."""

    source: str
    source_id: str
    title: str
    company: str
    location: str = ""
    url: str
    posted_at: str | None = None  # ISO date/datetime as given by the source
    posted_text: str | None = None  # e.g. "2 hours ago"
    company_url: str | None = None

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"

    @property
    def company_key(self) -> str:
        return company_norm(self.company)


class JobDetail(BaseModel):
    description: str = ""
    seniority: str | None = None
    employment_type: str | None = None
    job_function: str | None = None
    industries: str | None = None
    applicants: str | None = None  # guest page range, e.g. "Be among the first 25 applicants", "Over 200 applicants"
