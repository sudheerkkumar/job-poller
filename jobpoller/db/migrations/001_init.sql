-- job-poller schema v1. Applied automatically on start (jobpoller/db/migrate.py).

CREATE TABLE jobs (
    job_key         TEXT PRIMARY KEY,              -- "<source>:<source_id>", e.g. "greenhouse:stripe:123"
    source          TEXT NOT NULL,                 -- linkedin | greenhouse | lever | ... | careers
    source_id       TEXT NOT NULL,
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    company_norm    TEXT NOT NULL,
    location        TEXT,
    url             TEXT NOT NULL,
    posted_at       TEXT,
    posted_text     TEXT,
    search_label    TEXT,
    status          TEXT NOT NULL,                 -- passed | rejected | duplicate
    reject_reason   TEXT,
    detail_fetched  BOOLEAN NOT NULL DEFAULT FALSE,
    seniority       TEXT,
    employment_type TEXT,
    job_function    TEXT,
    industries      TEXT,
    applicants      TEXT,
    description     TEXT,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX jobs_role ON jobs (company_norm, lower(title));
CREATE INDEX jobs_pending ON jobs (first_seen_at) WHERE status = 'passed';

CREATE TABLE scores (
    job_key    TEXT PRIMARY KEY REFERENCES jobs (job_key) ON DELETE CASCADE,
    score      INTEGER NOT NULL,
    matched    JSONB,
    gaps       JSONB,
    red_flags  JSONB,
    one_liner  TEXT,
    model      TEXT NOT NULL,
    scored_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX scores_score ON scores (score);

CREATE TABLE notifications (
    job_key  TEXT PRIMARY KEY,                     -- one message per job, ever
    channel  TEXT NOT NULL,
    sent_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE llm_calls (
    id              BIGSERIAL PRIMARY KEY,
    job_key         TEXT,
    provider        TEXT NOT NULL,
    model           TEXT,
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome         TEXT NOT NULL,                 -- ok | invalid | unavailable
    prompt_tokens   INTEGER,
    output_tokens   INTEGER,
    thoughts_tokens INTEGER,
    error           TEXT
);
CREATE INDEX llm_calls_at ON llm_calls (at);

-- every company seen in any source (input for job-board discovery)
CREATE TABLE companies (
    company_norm  TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    careers_url   TEXT,
    added_by      TEXT NOT NULL DEFAULT 'seen',    -- seen (from a job) | config (you listed it)
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    jobs_seen     INTEGER NOT NULL DEFAULT 0,
    jobs_passed   INTEGER NOT NULL DEFAULT 0
);

-- job boards being watched
CREATE TABLE boards (
    id        TEXT PRIMARY KEY,                    -- "<ats>:<slug>"
    ats       TEXT NOT NULL,
    slug      TEXT NOT NULL,
    company   TEXT NOT NULL,
    host      TEXT,                                -- lever: api.lever.co | api.eu.lever.co; greenhouse: eu; personio: de | com
    status    TEXT NOT NULL DEFAULT 'active',      -- active | paused | dead
    added_by  TEXT NOT NULL DEFAULT 'manual',      -- seed | config | discovery | careers | manual
    added_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    url       TEXT,
    note      TEXT
);

-- poller-owned state per board: ids already seen (the first poll is a baseline, never alerted)
CREATE TABLE board_state (
    board_id        TEXT PRIMARY KEY REFERENCES boards (id) ON DELETE CASCADE,
    known_ids       TEXT[],                        -- NULL = never polled successfully (next ok poll = baseline)
    jobs            INTEGER,
    failures        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    first_polled_at TIMESTAMPTZ,
    last_ok_at      TIMESTAMPTZ,
    dead_at         TIMESTAMPTZ
);

-- discovery result per company
CREATE TABLE discovery (
    company_norm TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    status       TEXT NOT NULL,                    -- found | none | error
    providers    TEXT[] NOT NULL DEFAULT '{}',     -- providers that answered for this company
    boards       TEXT[] NOT NULL DEFAULT '{}',
    checked_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE search_state (
    search_key      TEXT PRIMARY KEY,
    last_success_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE source_state (
    source TEXT PRIMARY KEY,
    data   JSONB NOT NULL DEFAULT '{}'             -- backoff_until, consecutive_failures, cycles, alerts ...
);

CREATE TABLE request_log (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL,                     -- search | detail | canary
    at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    http_status INTEGER                            -- NULL = network error
);
CREATE INDEX request_log_at ON request_log (source, at);

CREATE TABLE poll_runs (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome     TEXT,
    details     JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX poll_runs_started ON poll_runs (started_at);
