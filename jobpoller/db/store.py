"""PostgreSQL storage (psycopg 3). One Store per process; every method is a short transaction (autocommit)."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..models import Job, JobDetail

log = logging.getLogger("jobpoller")
MIGRATIONS = Path(__file__).parent / "migrations"


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(s: str | datetime | None) -> datetime | None:
    if s is None or isinstance(s, datetime):
        return s
    return datetime.fromisoformat(s)


def _ts(v):
    """Accept ISO strings (as the ported runners pass them) or datetimes."""
    return parse_iso(v) if isinstance(v, str) else v


class Store:
    def __init__(self, url: str, migrate: bool = True):
        self.conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        if migrate:
            self.migrate()

    def close(self) -> None:
        self.conn.close()

    # ---- schema -------------------------------------------------------------
    def migrate(self) -> list[str]:
        """Apply migrations/NNN_*.sql that are not applied yet, each in its own transaction.
        Several containers may start at once (poller + linkedin): an advisory lock makes them take turns."""
        self.conn.execute("SELECT pg_advisory_lock(hashtext('jobpoller:migrate'))")
        try:
            return self._migrate()
        finally:
            self.conn.execute("SELECT pg_advisory_unlock(hashtext('jobpoller:migrate'))")

    def try_lock(self, name: str) -> bool:
        """Session advisory lock without waiting: True if this connection now holds `name`."""
        r = self._one("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (f"jobpoller:{name}",))
        return bool(r and r["ok"])

    def unlock(self, name: str) -> None:
        self.conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"jobpoller:{name}",))

    def _migrate(self) -> list[str]:
        self.conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                          "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        done = {r["version"] for r in self.conn.execute("SELECT version FROM schema_migrations")}
        applied = []
        for f in sorted(MIGRATIONS.glob("*.sql")):
            if f.stem in done:
                continue
            with self.conn.transaction():
                self.conn.execute(f.read_text(encoding="utf-8"))
                self.conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (f.stem,))
            applied.append(f.stem)
            log.info("db: applied migration %s", f.stem)
        return applied

    def _one(self, q: str, args=()) -> dict | None:
        return self.conn.execute(q, args).fetchone()

    def _all(self, q: str, args=()) -> list[dict]:
        return self.conn.execute(q, args).fetchall()

    # ---- jobs ---------------------------------------------------------------
    def is_seen(self, job_key: str) -> bool:
        return self._one("SELECT 1 AS x FROM jobs WHERE job_key=%s", (job_key,)) is not None

    def insert_job(self, job: Job, *, status: str, reason: str | None, search_label: str,
                   detail: JobDetail | None = None) -> None:
        d = detail or JobDetail()
        with self.conn.transaction():
            cur = self.conn.execute(
                """INSERT INTO jobs (job_key, source, source_id, title, company, company_norm, location, url,
                       posted_at, posted_text, search_label, status, reject_reason, detail_fetched, seniority,
                       employment_type, job_function, industries, description, applicants)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (job_key) DO NOTHING""",
                (job.key, job.source, job.source_id, job.title, job.company, job.company_key, job.location,
                 job.url, job.posted_at, job.posted_text, search_label, status, reason, detail is not None,
                 d.seniority, d.employment_type, d.job_function, d.industries, d.description or None, d.applicants))
            if cur.rowcount:
                self.conn.execute("UPDATE companies SET jobs_seen = jobs_seen + 1, jobs_passed = jobs_passed + %s "
                                  "WHERE company_norm=%s", (1 if status == "passed" else 0, job.company_key))

    def get_job(self, job_key: str) -> dict | None:
        return self._one("SELECT j.*, s.score, s.one_liner FROM jobs j LEFT JOIN scores s USING (job_key) "
                         "WHERE job_key=%s", (job_key,))

    # ---- companies ----------------------------------------------------------
    def touch_company(self, job: Job) -> None:
        self.conn.execute(
            """INSERT INTO companies (company_norm, name) VALUES (%s,%s)
               ON CONFLICT (company_norm) DO UPDATE SET last_seen_at = now()""",
            (job.company_key, job.company))

    def add_config_company(self, company_norm: str, name: str, careers_url: str | None) -> None:
        self.conn.execute(
            """INSERT INTO companies (company_norm, name, careers_url, added_by) VALUES (%s,%s,%s,'config')
               ON CONFLICT (company_norm) DO UPDATE SET careers_url = COALESCE(EXCLUDED.careers_url,
                   companies.careers_url), added_by = 'config'""",
            (company_norm, name, careers_url))

    def companies_for_discovery(self) -> list[dict]:
        return self._all("SELECT company_norm AS company_key, name, careers_url, added_by, jobs_passed, "
                         "last_seen_at FROM companies")

    # ---- search / source state ---------------------------------------------
    def last_success(self, search_key: str) -> datetime | None:
        r = self._one("SELECT last_success_at FROM search_state WHERE search_key=%s", (search_key,))
        return r["last_success_at"] if r else None

    def mark_success(self, search_key: str, at: datetime) -> None:
        self.conn.execute(
            """INSERT INTO search_state (search_key, last_success_at) VALUES (%s,%s)
               ON CONFLICT (search_key) DO UPDATE SET last_success_at = EXCLUDED.last_success_at""",
            (search_key, _ts(at)))

    def source_state(self, source: str) -> dict:
        r = self._one("SELECT data FROM source_state WHERE source=%s", (source,))
        return {"source": source, "backoff_until": None, "consecutive_failures": 0, "cycles": 0,
                **((r or {}).get("data") or {})}

    def update_source_state(self, source: str, **fields) -> None:
        self.conn.execute(
            """INSERT INTO source_state (source, data) VALUES (%s, %s)
               ON CONFLICT (source) DO UPDATE SET data = source_state.data || EXCLUDED.data""",
            (source, Jsonb(json.loads(json.dumps(fields, default=str)))))

    # ---- request log --------------------------------------------------------
    def log_request(self, source: str, kind: str, http_status: int | None) -> None:
        self.conn.execute("INSERT INTO request_log (source, kind, http_status) VALUES (%s,%s,%s)",
                          (source, kind, http_status))

    def requests_since(self, source: str, since: datetime, kind: str | None = None) -> int:
        q, args = "SELECT count(*) AS n FROM request_log WHERE source=%s AND at>=%s", [source, since]
        if kind:
            q += " AND kind=%s"
            args.append(kind)
        return self._one(q, args)["n"]

    # ---- scoring ------------------------------------------------------------
    def pending_for_scoring(self, max_failures: int) -> list[dict]:
        """Stage 1 passes with a description, not scored yet, not given up after repeated invalid answers."""
        return self._all(
            """SELECT j.* FROM jobs j
               WHERE j.status='passed' AND j.detail_fetched AND j.description IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM scores s WHERE s.job_key=j.job_key)
                 AND (SELECT count(*) FROM llm_calls c WHERE c.job_key=j.job_key AND c.outcome='invalid') < %s
               ORDER BY j.first_seen_at""", (max_failures,))

    def save_score(self, job_key: str, **f) -> None:
        j = lambda v: Jsonb(json.loads(v) if isinstance(v, str) else v)
        self.conn.execute(
            """INSERT INTO scores (job_key, score, matched, gaps, red_flags, one_liner, model)
               VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (job_key) DO NOTHING""",
            (job_key, f["score"], j(f["matched"]), j(f["gaps"]), j(f["red_flags"]), f["one_liner"], f["model"]))

    def log_llm_call(self, job_key: str | None, provider: str, model: str | None, outcome: str, *,
                     prompt_tokens: int | None = None, output_tokens: int | None = None,
                     thoughts_tokens: int | None = None, error: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO llm_calls (job_key, provider, model, outcome, prompt_tokens, output_tokens,
                   thoughts_tokens, error) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (job_key, provider, model, outcome, prompt_tokens, output_tokens, thoughts_tokens, error))

    def llm_calls_since(self, since: datetime) -> int:
        return self._one("SELECT count(*) AS n FROM llm_calls WHERE at>=%s", (since,))["n"]

    # ---- dedupe (reposts get new ids) ---------------------------------------
    def same_role(self, company_norm: str, title: str, scored_only: bool = False) -> str | None:
        q = ("SELECT j.job_key FROM jobs j WHERE j.company_norm=%s AND lower(j.title)=lower(%s) "
             "AND j.status='passed'")
        if scored_only:
            q += " AND EXISTS (SELECT 1 FROM scores s WHERE s.job_key=j.job_key)"
        r = self._one(q + " ORDER BY j.first_seen_at LIMIT 1", (company_norm, title))
        return r["job_key"] if r else None

    def mark_duplicate(self, job_key: str, of: str) -> None:
        self.conn.execute("UPDATE jobs SET status='duplicate', reject_reason=%s WHERE job_key=%s",
                          (f"duplicate_of:{of}", job_key))

    # ---- notifications ------------------------------------------------------
    def notified(self, job_key: str) -> bool:
        return self._one("SELECT 1 AS x FROM notifications WHERE job_key=%s", (job_key,)) is not None

    def notified_same_role(self, company_norm: str, title: str) -> str | None:
        r = self._one("""SELECT n.job_key FROM notifications n JOIN jobs j USING (job_key)
                         WHERE j.company_norm=%s AND lower(j.title)=lower(%s) LIMIT 1""", (company_norm, title))
        return r["job_key"] if r else None

    def mark_notified(self, job_key: str, channel: str) -> None:
        self.conn.execute("INSERT INTO notifications (job_key, channel) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                          (job_key, channel))

    def unnotified_matches(self, min_score: int) -> list[dict]:
        return self._all(
            """SELECT j.*, s.score, s.matched, s.gaps, s.red_flags, s.one_liner FROM jobs j JOIN scores s USING (job_key)
               WHERE s.score >= %s AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.job_key=j.job_key)
               ORDER BY s.scored_at""", (min_score,))

    def matches(self, min_score: int, limit: int = 50) -> list[dict]:
        return self._all(
            """SELECT j.title, j.company, j.location, j.url, j.source, s.score, s.one_liner, s.scored_at
               FROM jobs j JOIN scores s USING (job_key) WHERE s.score >= %s ORDER BY s.scored_at DESC LIMIT %s""",
            (min_score, limit))

    # ---- poll runs ----------------------------------------------------------
    def record_run(self, **fields) -> None:
        source = fields.pop("source", "?")
        outcome = fields.pop("outcome", None)
        started = _ts(fields.pop("started_at", None)) or utcnow()
        self.conn.execute("INSERT INTO poll_runs (source, started_at, outcome, details) VALUES (%s,%s,%s,%s)",
                          (source, started, outcome, Jsonb(json.loads(json.dumps(fields, default=str)))))

    # ---- boards -------------------------------------------------------------
    def list_boards(self, status: str | None = None) -> list[dict]:
        if status:
            return self._all("SELECT * FROM boards WHERE status=%s ORDER BY id", (status,))
        return self._all("SELECT * FROM boards ORDER BY id")

    def add_boards(self, boards: list[dict]) -> int:
        """Insert boards that don't exist yet. Returns how many were added."""
        n = 0
        with self.conn.transaction():
            for b in boards:
                cur = self.conn.execute(
                    """INSERT INTO boards (id, ats, slug, company, host, status, added_by, url, note)
                       VALUES (%(id)s, %(ats)s, %(slug)s, %(company)s, %(host)s, %(status)s, %(added_by)s, %(url)s,
                               %(note)s) ON CONFLICT (id) DO NOTHING""",
                    {"host": None, "status": "active", "added_by": "manual", "url": None, "note": None, **b})
                n += cur.rowcount
        return n

    def set_board_status(self, board_id: str, status: str, note: str | None = None) -> None:
        self.conn.execute("UPDATE boards SET status=%s, note=COALESCE(%s, note) WHERE id=%s",
                          (status, note, board_id))

    def board_states(self) -> dict[str, dict]:
        return {r["board_id"]: r for r in self._all("SELECT * FROM board_state")}

    def save_board_state(self, board_id: str, **f) -> None:
        cols = ["known_ids", "jobs", "failures", "last_error", "first_polled_at", "last_ok_at", "dead_at"]
        vals = {c: f.get(c) for c in cols}
        sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c in f)
        self.conn.execute(
            f"""INSERT INTO board_state (board_id, {', '.join(cols)})
                VALUES (%(board_id)s, {', '.join(f'%({c})s' for c in cols)})
                ON CONFLICT (board_id) DO UPDATE SET {sets or 'failures = board_state.failures'}""",
            {"board_id": board_id, **vals, "failures": f.get("failures", 0)})

    # ---- discovery ----------------------------------------------------------
    def discovery(self) -> dict[str, dict]:
        return {r["company_norm"]: r for r in self._all("SELECT * FROM discovery")}

    def save_discovery(self, company_norm: str, name: str, status: str, providers: list[str],
                       boards: list[str]) -> None:
        self.conn.execute(
            """INSERT INTO discovery (company_norm, name, status, providers, boards, checked_at)
               VALUES (%s,%s,%s,%s,%s,now())
               ON CONFLICT (company_norm) DO UPDATE SET name=EXCLUDED.name, status=EXCLUDED.status,
                   providers=EXCLUDED.providers, boards=EXCLUDED.boards, checked_at=now()""",
            (company_norm, name, status, providers, boards))

    # ---- stats --------------------------------------------------------------
    def counts(self) -> dict:
        r = self._one("""SELECT
            (SELECT count(*) FROM jobs) AS jobs,
            (SELECT count(*) FROM jobs WHERE status='passed') AS passed,
            (SELECT count(*) FROM scores) AS scored,
            (SELECT count(*) FROM notifications) AS notified,
            (SELECT count(*) FROM boards WHERE status='active') AS boards_active,
            (SELECT count(*) FROM companies) AS companies""")
        return dict(r)
