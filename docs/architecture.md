# Architecture

## Containers

| Service | Image | Runs | Started by |
|---|---|---|---|
| `db` | postgres:16-alpine | PostgreSQL, data in `./data/pg` | `docker compose up -d` |
| `poller` | job-poller | `run --skip linkedin`: board polls, discovery, scoring, alerts | `docker compose up -d` |
| `linkedin` | job-poller (same image) | `run --only linkedin`: LinkedIn cycles, scoring of what it finds | `docker compose --profile linkedin up -d` (opt-in) |

Shared database, two writers: migrations take a PostgreSQL advisory lock at start-up, and scoring runs under a
second advisory lock (`pg_try_advisory_lock`) - whichever process holds it scores, the other skips and leaves the
jobs to it. Inserts are idempotent (`ON CONFLICT DO NOTHING`), so the rest needs no coordination.

## Components

| Module | Responsibility |
|---|---|
| `jobpoller/app.py` | start-up sync (seed, config companies/boards) and the scheduler loop |
| `jobpoller/sources/boards.py` | readers for 7 job-board providers (+ EU hosts), slug guessing, name-verified probing |
| `jobpoller/sources/careers.py` | finds the job board behind a careers page (robots.txt-aware, ≤ 2 hops, no JS) |
| `jobpoller/sources/linkedin.py` | opt-in LinkedIn guest search client: parsing, block detection, pacing |
| `jobpoller/boards_runner.py` | board polling (baseline, new ids, failures → dead) and discovery |
| `jobpoller/linkedin_runner.py` | LinkedIn cycles: time windows, hourly request budget, backoff, canary |
| `jobpoller/filters.py` | Stage 1 rules |
| `jobpoller/scorer.py` | Stage 2: LLM scoring with structured output, model fallback, retries, daily cap |
| `jobpoller/notify/` | console + Telegram, one message per job |
| `jobpoller/db/` | PostgreSQL store and numbered SQL migrations |
| `jobpoller/seed_refresh.py` | rebuilds `seed/boards.json` from the Common Crawl URL index |

## Data model (PostgreSQL)

- `jobs` - every job seen, including rejects (with the reason) and duplicates. Key `<source>:<id>`.
- `scores`, `notifications`, `llm_calls` - LLM results, what was sent where, token usage.
- `companies` - every company seen in any source (input for discovery), plus companies from config.
- `boards`, `board_state`, `discovery` - watched boards, the ids each board has shown (baseline + new-job
  detection), and per-company discovery results.
- `search_state`, `source_state`, `request_log`, `poll_runs` - LinkedIn windows/backoff/budget and run history.

## Flow of one board poll

1. Fetch every active board in parallel over one shared HTTP client (one request per board).
2. Board never polled successfully → store its current job ids as a **baseline**, alert nothing.
3. Otherwise, for each job id not seen on this board: Stage 1 rules → already stored? → same company + title
   already passed (repost)? → fetch the description if the list doesn't include it → store as `passed`.
4. Errors count per board; after `dead_after_failures` consecutive failures the board is marked `dead`.
5. After the poll, pending jobs are scored; scores ≥ threshold are notified once.

## Why these choices

- **Board APIs over scraping**: structured, stable, sanctioned, cheap (one request per board).
- **Baseline on first poll**: adding 150 boards must not produce 5,000 alerts.
- **Two-stage filtering**: local rules discard most jobs for free; the LLM only sees plausible ones.
- **Structured LLM output validated with pydantic**: no free-text parsing; invalid answers are retried, then the
  job is given up after `max_attempts_per_job` and logged.
- **Postgres in Compose**: several kinds of writers, transactions and constraints instead of file locking; data in
  a bind-mounted folder you can back up with `pg_dump`.
- **LinkedIn opt-in with floors in code**: the default experience uses only official APIs; anyone enabling
  LinkedIn gets a deliberately slow, honest client.
