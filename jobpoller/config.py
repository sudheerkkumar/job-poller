"""Configuration: config.yaml (what to look for) + environment variables (secrets, database URL).

Every section has safe defaults, so a minimal config.yaml only needs `regions`, `filters` and `scorer.profile`.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

# LinkedIn guest search is opt-in and deliberately slow. These floors cannot be lowered from config.
LINKEDIN_MIN_JITTER_SECONDS = 15.0
LINKEDIN_MIN_INTERVAL_MINUTES = 30
LINKEDIN_MAX_REQUESTS_PER_HOUR = 60


class Search(BaseModel):
    keywords: str
    location: str

    @property
    def key(self) -> str:
        return f"{self.keywords.strip().lower()}|{self.location.strip().lower()}"

    @property
    def label(self) -> str:
        return f"{self.keywords} @ {self.location}"


class Canary(BaseModel):
    keywords: str = "software engineer"
    location: str = "Spain"
    window_seconds: int = 86400
    every_n_cycles: int = 6


class DailyCatchup(BaseModel):
    enabled: bool = False
    at: str = "10:00"                      # local time, once per day
    timezone: str = "UTC"
    window_seconds: int = 86400
    extra_params: dict[str, str] = Field(default_factory=lambda: {"f_EA": "true"})  # "under 10 applicants"


class LinkedInConfig(BaseModel):
    """OPT-IN. LinkedIn's terms do not allow automated access; you are responsible for how you use this.
    Public guest pages only: no login, no cookies, honest User-Agent, slow fixed pacing, stops on HTTP 429."""
    enabled: bool = False
    interval_minutes: int = 30
    align_to_clock: bool = True
    clock_offset_minutes: int = 0
    initial_window_seconds: int = 3600
    base_window_seconds: int = 3600
    max_window_seconds: int = 86400
    max_pages: int = 0                     # 0 = until a short page
    max_requests_per_hour: int = 30
    jitter_seconds: tuple[float, float] = (15.0, 25.0)
    backoff_minutes: list[int] = Field(default_factory=lambda: [60, 120])
    fetch_details: bool = True
    max_details_per_cycle: int = 0
    searches: list[Search] = Field(default_factory=list)
    canary: Canary = Field(default_factory=Canary)
    daily_catchup: DailyCatchup = Field(default_factory=DailyCatchup)

    @field_validator("interval_minutes")
    @classmethod
    def _interval_floor(cls, v: int) -> int:
        return max(v, LINKEDIN_MIN_INTERVAL_MINUTES)

    @field_validator("jitter_seconds")
    @classmethod
    def _jitter_floor(cls, v: tuple[float, float]) -> tuple[float, float]:
        lo = max(v[0], LINKEDIN_MIN_JITTER_SECONDS)
        return lo, max(v[1], lo)

    @field_validator("max_requests_per_hour")
    @classmethod
    def _cap(cls, v: int) -> int:
        return LINKEDIN_MAX_REQUESTS_PER_HOUR if v <= 0 else min(v, LINKEDIN_MAX_REQUESTS_PER_HOUR)


class Filters(BaseModel):
    title_include_any: list[str] = Field(default_factory=list)
    title_exclude_any: list[str] = Field(default_factory=list)
    locations_accepted_any: list[str] = Field(default_factory=list)
    remote_candidate_locations: list[str] = Field(default_factory=list)
    remote_keywords_any: list[str] = Field(default_factory=list)
    # job boards: a remote job passes when its location names one of these (plain "Remote" is mostly US-only)
    remote_regions_any: list[str] = Field(default_factory=lambda: ["europe", "emea", "eu", "european union", "cet"])
    company_blocklist: list[str] = Field(default_factory=list)
    seniority_exclude: list[str] = Field(default_factory=list)
    employment_type_exclude: list[str] = Field(default_factory=list)


class ScorerConfig(BaseModel):
    enabled: bool = True
    provider: str = "gemini"
    models: list[str] = Field(default_factory=lambda: ["gemini-3.5-flash-lite", "gemini-3.6-flash",
                                                       "gemini-3.1-flash-lite"])
    unavailable_retry_seconds: list[int] = Field(default_factory=lambda: [30, 60])
    min_seconds_between_calls: float = 5.0
    max_calls_per_cycle: int = 40
    max_calls_per_day: int = 300
    max_attempts_per_job: int = 2
    description_max_chars: int = 8000
    alert_threshold: int = 7
    poll_seconds: int = 60
    profile: str = ""
    criteria: str = ""


class BoardsConfig(BaseModel):
    """Company job boards (Greenhouse, Lever, Ashby, SmartRecruiters, Personio, Teamtailor, Recruitee)."""
    enabled: bool = True
    interval_minutes: int = 15
    providers: list[str] = Field(default_factory=lambda: ["greenhouse", "lever", "ashby", "smartrecruiters",
                                                          "personio", "teamtailor", "recruitee"])
    workers: int = 8
    timeout_seconds: float = 20.0
    dead_after_failures: int = 96          # ~1 day of 15-min polls failing -> board marked dead
    seed: bool = True                      # load the shipped starting list (seed/boards.json) for your regions
    discover_interval_hours: int = 24
    discover_max_companies: int = 250
    discover_recheck_none_days: int = 30


class Company(BaseModel):
    """A company you want watched: a name (board is guessed) and/or a careers page URL (board is detected)."""
    name: str
    careers_url: str | None = None


class NotifyConfig(BaseModel):
    console: bool = True
    min_score: int | None = None           # default: scorer.alert_threshold
    # Telegram is on when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set in .env


class Config(BaseModel):
    # regions decide which seed boards are watched: names from seed/regions.yaml (e.g. spain, europe-remote)
    regions: list[str] = Field(default_factory=lambda: ["spain"])
    companies: list[Company] = Field(default_factory=list)
    boards: list[str] = Field(default_factory=list)   # direct board ids, e.g. "greenhouse:stripe"
    linkedin: LinkedInConfig = Field(default_factory=LinkedInConfig)  # gitleaks:allow (field name, not a secret)
    job_boards: BoardsConfig = Field(default_factory=BoardsConfig)
    filters: Filters = Field(default_factory=Filters)
    scorer: ScorerConfig = Field(default_factory=ScorerConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    timezone: str = "UTC"


def config_path() -> Path:
    return Path(os.environ.get("JOBPOLLER_CONFIG", "config.yaml"))


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://jobpoller:jobpoller@localhost:5432/jobpoller")


def load_config(path: Path | None = None) -> Config:
    with (path or config_path()).open(encoding="utf-8") as f:
        return Config.model_validate(yaml.safe_load(f) or {})
