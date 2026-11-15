"""Stage 2: LLM scoring of Stage 1 survivors against the candidate profile + criteria (config.yaml `scorer`).

Provider is pluggable (`Scorer` protocol). Implemented: Gemini (has a free tier). Jobs are never lost: when the
provider is unavailable / out of quota, they stay pending and are scored on a later cycle.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from pydantic import BaseModel, Field, ValidationError

from .config import ScorerConfig
from .db import Store, utcnow

log = logging.getLogger("jobpoller")


class JobScore(BaseModel):
    score: int = Field(ge=0, le=10, description="0-10. 7+ means worth applying today.")
    matched: list[str] = Field(description="Short facts from the posting that match the candidate/criteria.")
    gaps: list[str] = Field(description="Requirements in the posting the candidate does not meet or criteria misses.")
    red_flags: list[str] = Field(description="Deal-breakers or warning signs stated in the posting.")
    one_liner: str = Field(description="One sentence verdict.")


@dataclass
class ScoreResult:
    score: JobScore
    model: str
    prompt_tokens: int | None
    output_tokens: int | None
    thoughts_tokens: int | None


class Unavailable(Exception):
    """Provider can't score right now (quota, overload, network). Stop for this cycle; jobs stay pending."""


class InvalidOutput(Exception):
    """Model answered but the answer didn't validate as JobScore."""


class Scorer(Protocol):
    def score(self, system: str, prompt: str) -> ScoreResult: ...


SYSTEM_PROMPT = """You are a strict job screener for one specific candidate.
Score how worth it is for THIS candidate to apply to THIS job today, 0-10:
  9-10 excellent fit, apply now; 7-8 good fit, worth applying; 4-6 partial fit; 0-3 poor fit or a deal-breaker.
Rules:
- Use only facts stated in the job posting. Do not assume requirements or benefits that are not written.
- Apply the candidate's MUST-HAVES and DEAL-BREAKERS strictly. Any deal-breaker present => score <= 3.
- Penalise: location/remote mismatch (the job must be workable from where the criteria say), seniority mismatch,
  required languages or core skills the candidate lacks, a primary stack far from the candidate's.
- If the posting is vague, say so in gaps and do not reward it.
- Keep matched/gaps/red_flags items short (a few words each), max 6 items per list."""


def build_prompt(job, cfg: ScorerConfig) -> str:
    desc = (job["description"] or "").strip()
    if len(desc) > cfg.description_max_chars:
        desc = desc[: cfg.description_max_chars] + "\n[...truncated]"
    meta = "\n".join(
        f"{k}: {v}" for k, v in [
            ("Title", job["title"]), ("Company", job["company"]), ("Location", job["location"]),
            ("Seniority", job.get("seniority")), ("Employment type", job.get("employment_type")),
            ("Posted", job.get("posted_text")),
        ] if v
    )
    return (f"CANDIDATE PROFILE:\n{cfg.profile.strip()}\n\nCANDIDATE CRITERIA:\n{cfg.criteria.strip()}\n\n"
            f"JOB POSTING:\n{meta}\n\nDescription:\n{desc}")


class GeminiScorer:
    """Gemini API. Tries models in order; a model that is overloaded/out of quota is skipped for the rest of
    the run. Raises Unavailable when no model is usable."""

    def __init__(self, cfg: ScorerConfig, api_key: str | None = None):
        from google import genai  # imported lazily so Stage 1 works without the SDK
        from google.genai import types

        key = (api_key if api_key is not None else os.environ.get("GEMINI_API_KEY", "")).strip()
        if not key:
            raise Unavailable("no Gemini API key (set GEMINI_API_KEY in .env)")
        # 60 s per call: without a timeout a call interrupted by host sleep can hang forever
        self.client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=60_000))
        self.models = list(cfg.models)
        self.dead: set[str] = set()

    def score(self, system: str, prompt: str) -> ScoreResult:
        from google.genai import errors, types

        last = None
        for model in self.models:
            if model in self.dead:
                continue
            try:
                r = self.client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        response_mime_type="application/json",
                        response_schema=JobScore,
                        temperature=0.2,
                    ),
                )
            except errors.APIError as e:
                last = f"{model}: HTTP {e.code} {e.status or ''} {(e.message or '')[:160]}"
                if e.code in (429, 500, 502, 503, 504):
                    log.warning("scorer: %s - trying next model", last)
                    self.dead.add(model)
                    continue
                raise InvalidOutput(last) from e  # 400 etc: problem with this request, not the provider
            except Exception as e:  # network errors etc.
                last = f"{model}: {type(e).__name__}: {str(e)[:160]}"
                log.warning("scorer: %s - trying next model", last)
                self.dead.add(model)
                continue
            u = r.usage_metadata
            parsed = r.parsed
            if not isinstance(parsed, JobScore):
                try:
                    parsed = JobScore.model_validate_json(r.text or "")
                except ValidationError as e:
                    raise InvalidOutput(f"{model}: invalid JSON: {str(e)[:160]}") from e
            return ScoreResult(
                score=parsed, model=model,
                prompt_tokens=getattr(u, "prompt_token_count", None),
                output_tokens=getattr(u, "candidates_token_count", None),
                thoughts_tokens=getattr(u, "thoughts_token_count", None),
            )
        raise Unavailable(last or "all models unavailable")


def make_scorer(cfg: ScorerConfig, api_key: str | None = None) -> Scorer:
    if cfg.provider == "gemini":
        return GeminiScorer(cfg, api_key)
    raise Unavailable(f"scorer provider {cfg.provider!r} not implemented")


# A call may take up to 60 s per model (HttpOptions timeout) and falls back to a 2nd model: never start one with
# less than this left before the deadline.
CALL_WORST_CASE_SECONDS = 130


def _score_with_retry(scorer, cfg: ScorerConfig, db, job, deadline: float | None):
    """All models busy (503/429/timeouts) -> wait `unavailable_retry_seconds` (30, 60) and try every model again,
    as long as the run deadline allows. Google's own 503 says spikes are usually temporary."""
    waits = list(cfg.unavailable_retry_seconds)
    while True:
        try:
            return scorer.score(SYSTEM_PROMPT, build_prompt(job, cfg))
        except Unavailable as e:
            if not waits:
                raise
            wait = waits.pop(0)
            if deadline is not None and time.monotonic() + wait + CALL_WORST_CASE_SECONDS > deadline:
                raise
            db.log_llm_call(job["job_key"], cfg.provider, None, "unavailable", error=str(e))
            log.warning("scorer: all models busy - retrying in %d s", wait)
            time.sleep(wait)
            if hasattr(scorer, "dead"):
                scorer.dead.clear()


def score_pending(cfg: ScorerConfig, db: Store, limit: int | None = None, deadline: float | None = None,
                  api_key: str | None = None, min_notify: int | None = None, console: bool = True) -> dict:
    """Score Stage 1 survivors that have a description and no score yet; notify matches. Returns counters.
    `deadline` (time.monotonic() value): no new call starts unless it can finish before it."""
    stats = {"scored": 0, "matches": 0, "invalid": 0, "pending": 0, "stopped": None, "reason": None}
    if not cfg.enabled:
        return stats
    used_today = db.llm_calls_since(utcnow() - timedelta(days=1))
    budget = min(cfg.max_calls_per_cycle if limit is None else limit, max(0, cfg.max_calls_per_day - used_today))
    jobs = db.pending_for_scoring(max_failures=cfg.max_attempts_per_job)
    stats["pending"] = len(jobs)
    if not jobs:
        return stats
    if budget <= 0:
        stats["stopped"] = f"daily LLM call cap reached ({cfg.max_calls_per_day}/24h)"
        stats["reason"] = "cap"
        log.warning("scorer: %s - %d jobs left pending", stats["stopped"], len(jobs))
        return stats
    try:
        scorer = make_scorer(cfg, api_key)
    except Unavailable as e:
        stats["stopped"] = str(e)
        stats["reason"] = "config"  # e.g. no API key: retrying won't help
        log.warning("scorer unavailable: %s - %d jobs left pending", e, len(jobs))
        return stats

    last_call = 0.0
    for job in jobs:
        if budget <= 0:
            stats["stopped"] = "call budget for this run used up"
            stats["reason"] = "run_budget"
            break
        wait = cfg.min_seconds_between_calls - (time.monotonic() - last_call)
        if deadline is not None and time.monotonic() + max(wait, 0) + CALL_WORST_CASE_SECONDS > deadline:
            stats["stopped"] = "run deadline reached"
            stats["reason"] = "deadline"
            break
        if wait > 0:
            time.sleep(wait)
        dup = db.same_role(job["company_norm"], job["title"], scored_only=True)
        if dup and dup != job["job_key"]:  # repost already scored: no LLM call
            db.mark_duplicate(job["job_key"], dup)
            stats["pending"] -= 1
            log.info("scorer: skip %s - duplicate of %s (same company + title)", job["job_key"], dup)
            continue
        last_call = time.monotonic()
        budget -= 1
        try:
            res = _score_with_retry(scorer, cfg, db, job, deadline)
        except Unavailable as e:
            db.log_llm_call(job["job_key"], cfg.provider, None, "unavailable", error=str(e))
            stats["stopped"] = str(e)
            stats["reason"] = "unavailable"
            log.warning("scorer stopped: %s - remaining jobs stay pending for the next cycle", e)
            break
        except InvalidOutput as e:
            db.log_llm_call(job["job_key"], cfg.provider, None, "invalid", error=str(e))
            stats["invalid"] += 1
            log.warning("scorer: invalid output for %s (%s)", job["job_key"], e)
            continue
        s = res.score
        db.log_llm_call(job["job_key"], cfg.provider, res.model, "ok", prompt_tokens=res.prompt_tokens,
                        output_tokens=res.output_tokens, thoughts_tokens=res.thoughts_tokens)
        db.save_score(job["job_key"], score=s.score, matched=json.dumps(s.matched, ensure_ascii=False),
                      gaps=json.dumps(s.gaps, ensure_ascii=False),
                      red_flags=json.dumps(s.red_flags, ensure_ascii=False),
                      one_liner=s.one_liner, model=res.model)
        stats["scored"] += 1
        log.info("scored %d/10 %s · %s (%s, %s+%s tok)", s.score, job["title"], job["company"], res.model,
                 res.prompt_tokens, res.output_tokens)
        if s.score >= (min_notify if min_notify is not None else cfg.alert_threshold):
            stats["matches"] += 1
            from .notify import notify_match
            notify_match(db, job, s.score, s.matched, s.gaps, s.red_flags, s.one_liner, console=console)
    stats["pending"] -= stats["scored"]
    return stats
