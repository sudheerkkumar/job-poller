# job-poller

[![ci](https://github.com/sudheerkkumar/job-poller/actions/workflows/ci.yml/badge.svg)](https://github.com/sudheerkkumar/job-poller/actions/workflows/ci.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
![PostgreSQL 16](https://img.shields.io/badge/postgres-16-336791)
![Docker Compose](https://img.shields.io/badge/docker-compose-2496ED)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

A self-hosted job radar. It watches company job boards for new openings, filters them with your rules, has an LLM
screen each survivor against **your** profile, and alerts you on Telegram - usually minutes after a job is posted.

It never applies to anything. It finds jobs and tells you why they fit (or don't).

```
8/10 · Senior Backend Engineer · Acme · [greenhouse]
Madrid, Spain
✓ Kotlin, Spring Boot, Kafka   ✗ Go nice-to-have   ⚠ -
» Strong match: senior backend role on the JVM, hybrid in Madrid.
https://job-boards.greenhouse.io/acme/jobs/123
```

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Tech stack](#tech-stack)
- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [LinkedIn (opt-in)](#linkedin-opt-in)
- [Everyday use](#everyday-use)
- [Commands](#commands)
- [Data, backups and reset](#data-backups-and-reset)
- [Updating](#updating)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Project layout](#project-layout)
- [Limitations](#limitations)
- [License](#license)

## Features

- **7 job-board providers** through their official public APIs: Greenhouse (US + EU), Lever (US + EU), Ashby,
  SmartRecruiters, Personio, Teamtailor, Recruitee. One request per board per poll, no keys, no logins.
- **Finds boards for you**, three ways:
  - a **seed list** shipped in the repo, built from the Common Crawl URL index and tagged by region
    (`spain`, `europe-remote`; rebuild it for other regions with one command);
  - **your companies**: give a careers page URL and job-poller finds the job board behind it (e.g. Aircall's
    careers page -> Greenhouse board `aircallioinc`, a slug nobody would guess);
  - **discovery**: every company seen in any source gets its likely board slugs probed and verified.
- **New jobs only**: the first poll of a board is a baseline - existing jobs are remembered, never alerted.
- **Two-stage filtering**: cheap local rules first, then an LLM (Google Gemini, free tier works) scores the
  survivors 0-10 with structured, validated output.
- **Alerts** in the console and on Telegram, one message per job, reposts not alerted twice.
- **LinkedIn guest search** as an **opt-in** separate container with slow pacing enforced in code
  ([how to use it](#linkedin-opt-in)).
- **Runs anywhere Docker runs**: two containers (PostgreSQL + the poller), a third one for LinkedIn only if you
  ask for it, data in a folder you can back up.

## How it works

```mermaid
flowchart LR
  subgraph find[Find boards]
    S[Seed list<br/>by region] --> B[(boards)]
    C[Your companies<br/>careers URL] -->|detect provider| B
    N[Companies seen<br/>in any source] -->|guess slug + verify name| B
  end
  B --> P[Poll every 15 min<br/>public board APIs]
  L[LinkedIn guest search<br/>opt-in] --> F
  P -->|new job ids only| F[Stage 1 rules<br/>title · location · company]
  F --> D[Dedupe reposts<br/>company + title]
  D --> G[Stage 2 LLM<br/>Gemini, structured output]
  G -->|score ≥ 7| T[Console / Telegram]
```

1. **Poll** - every 15 minutes, all active boards are fetched in parallel.
2. **Baseline** - a board seen for the first time only records its current job ids.
3. **Stage 1** - each *new* job goes through your rules: title include/exclude, accepted locations,
   remote-in-Europe, company blocklist. Rejects are stored with the reason and never looked at again.
4. **Dedupe** - the same company + title seen before (a repost) is skipped.
5. **Stage 2** - survivors are sent to Gemini with your profile and criteria. The answer is JSON
   (`score`, `matched`, `gaps`, `red_flags`, `one_liner`) validated with pydantic. If the LLM is busy or out of
   quota, jobs stay pending and are scored later - never lost.
6. **Notify** - score ≥ your threshold -> console line + Telegram message.

More detail in [docs/architecture.md](docs/architecture.md).

## Tech stack

| Area | Used |
|---|---|
| Language | Python 3.12 |
| HTTP / parsing | httpx, selectolax (HTML), PyYAML |
| Models / validation | pydantic v2 |
| Database | PostgreSQL 16, psycopg 3, plain numbered SQL migrations |
| LLM | Google Gemini via `google-genai` (structured output) |
| Notifications | Telegram Bot API |
| Runtime | Docker, Docker Compose |
| Quality | pytest, ruff, GitHub Actions, gitleaks secret scan |

## Prerequisites

| What | Why | Notes |
|---|---|---|
| **Docker** with **Compose v2** | runs everything | Docker Desktop (macOS / Windows) or Docker Engine + compose plugin (Linux). Check: `docker compose version` |
| **git** | to clone the repo | |
| **Gemini API key** (free) | Stage 2 scoring | Create at [aistudio.google.com/apikey](https://aistudio.google.com/apikey) with a Google account. No card needed for the free tier. |
| **Telegram bot** (optional) | phone alerts | ~3 minutes with @BotFather - see [docs/telegram.md](docs/telegram.md). Without it you get console alerts only. |
| ~1 GB free RAM, ~1 GB disk | containers + database | Memory limits: Postgres 256 MB, poller 512 MB. |

You do **not** need Python installed on your machine - it runs inside the container. Developed and tested on macOS
(Apple Silicon); the images are standard multi-arch (`python:3.12-slim`, `postgres:16-alpine`).

## Quick start

```bash
# 1. Get the code
git clone https://github.com/sudheerkkumar/job-poller.git
cd job-poller

# 2. Secrets: add GEMINI_API_KEY (and Telegram, if you want it)
cp .env.example .env
nano .env

# 3. What to look for: regions, filters, your profile
cp config.example.yaml config.yaml
nano config.yaml

# 4. Start Postgres + the poller in the background
docker compose up -d

# 5. Watch it work
docker compose logs -f poller
```

What you should see on the first start:

1. The database schema is created.
2. The seed boards for your `regions` are loaded, plus the companies and boards from `config.yaml`
   (careers pages are checked right away).
3. The first poll records a **baseline** for every board - no alerts yet, this is expected.
4. From the next poll (15 minutes later) on, new jobs that pass your rules are scored, and matches appear in the
   log and on Telegram.

Check your notification setup at any time:

```bash
docker compose run --rm poller python -m jobpoller test-notify
```

## Configuration

Two files, both gitignored:

### `.env` - secrets

| Variable | Required | What |
|---|---|---|
| `GEMINI_API_KEY` | for scoring | Without it, jobs that pass Stage 1 are stored and stay pending until a key is set. |
| `TELEGRAM_BOT_TOKEN` | no | Telegram is on when both Telegram variables are set. |
| `TELEGRAM_CHAT_ID` | no | Your chat id ([docs/telegram.md](docs/telegram.md)). |
| `POSTGRES_PASSWORD` | no | Database password (default `jobpoller`). The database is not exposed outside Docker. Set it **before** the first start - it is fixed when the data folder is created. |

### `config.yaml` - what to look for

Start from [config.example.yaml](config.example.yaml) - every option is commented. The main sections:

```yaml
regions: [spain, europe-remote]          # which seed boards to watch

companies:                               # companies you care about
  - { name: "Aircall", careers_url: "https://aircall.io/careers/" }   # board detected from the page
  - { name: "Stripe" }                                                 # board guessed from the name

boards:                                  # boards you already know: <provider>:<slug>[@eu]
  - greenhouse:stripe

filters:                                 # Stage 1, case- and accent-insensitive substring matches
  title_include_any: [backend, java, kotlin, software engineer]
  title_exclude_any: [junior, intern, frontend]
  locations_accepted_any: [spain, madrid, barcelona]
  remote_keywords_any: [remote, remoto]
  remote_regions_any: [europe, emea, eu]  # a remote job passes only if its location names one of these
  company_blocklist: []

scorer:                                  # Stage 2
  alert_threshold: 7
  profile: |
    Senior backend engineer, 8+ years. Java and Kotlin; Spring Boot, PostgreSQL, Kafka, AWS.
  criteria: |
    Senior or lead backend roles, workable from Spain. English or Spanish.

linkedin:
  enabled: false                         # opt-in, read docs/linkedin.md first
```

| Section | What it controls |
|---|---|
| `regions` | Which seed boards are loaded. The shipped seed has `spain` and `europe-remote`. `germany`, `uk` and `us` are defined in [seed/regions.yaml](seed/regions.yaml) but need a seed rebuild first (see [Commands](#commands)). |
| `companies` | Companies to watch. With `careers_url` the board is detected at start-up; with only `name` it is guessed by daily discovery. |
| `boards` | Board ids you already know, e.g. `greenhouse:stripe`, `lever:acme@eu`. Providers: `greenhouse`, `lever`, `ashby`, `smartrecruiters`, `personio`, `teamtailor`, `recruitee`. |
| `job_boards` | Poll interval (default 15 min), workers, when a failing board is marked dead, discovery settings. |
| `filters` | Stage 1 rules. Keep them loose-ish: the LLM is the precise filter, these just cut the noise. |
| `scorer` | Your profile and criteria (the only personal text sent to Gemini), alert threshold, model list, daily call cap. |
| `notify` | Console on/off, `min_score` if it should differ from `scorer.alert_threshold`. |
| `linkedin` | Opt-in guest search: `enabled`, `searches` (keywords + location) - see [LinkedIn (opt-in)](#linkedin-opt-in). |

**Tips for a good profile:** write it like the top of a CV (years, main languages, frameworks, leadership), and put
deal-breakers in `criteria` (location, language, seniority, role types you don't want). The LLM only judges from
the posting plus this text. Don't put private data (salary history, contacts) in it.

After changing `config.yaml`:

```bash
docker compose restart poller
```

## LinkedIn (opt-in)

job-poller can also poll LinkedIn's **public guest job search** - the pages anyone can open without logging in.
It runs in its **own container** and is **off by default**.

> **Read this first.** LinkedIn's User Agreement does not allow automated access. Turning this on is your decision
> and your responsibility. The job boards (the default source) use official public APIs and need none of this.

**What it does and doesn't do**

- Guest pages only: no login, no cookies kept, no browser automation, an honest `User-Agent` (`job-poller/...`).
- Slow, fixed pacing that config can only make slower: **≥ 15 s between requests, ≥ 30 min between cycles,
  ≤ 60 requests per hour**.
- On HTTP 429 (rate limited) or a redirect to a login page it **stops**, backs off (60, then 120 minutes) and
  widens the next search window so no time range is lost. It never retries around a block, rotates IPs or
  disguises itself.
- Jobs it finds go through the same Stage 1 rules, dedupe, LLM scoring and alerts as job-board jobs, tagged
  `[linkedin]`.

**Why a separate container:** you switch it on and off without touching the job boards, it has its own log and
memory limit, and when LinkedIn blocks it, nothing else is affected. Both containers share the database: the
hourly request budget is counted across both, and only one of them scores at a time (no double alerts).

### Turn it on

It takes **two switches**, on purpose - a config setting and a Compose profile:

1. In `config.yaml`, enable it and add at least one search:
   ```yaml
   linkedin:
     enabled: true
     searches:
       - { keywords: "senior (backend OR java OR kotlin)", location: "Spain" }
       - { keywords: "platform engineer", location: "European Union" }
   ```
2. Start the LinkedIn container (the other two keep running):
   ```bash
   docker compose --profile linkedin up -d
   ```
3. Watch it:
   ```bash
   docker compose logs -f linkedin
   ```
   On start you'll see a warning that LinkedIn polling is ON, then a cycle (all searches) every 30 minutes.
   The first cycle looks back 1 hour; after that each cycle covers the time since the last successful one.
   Every 6th cycle also runs one broad "canary" search, to tell "no new jobs" apart from "parsing broke".

### Search tips

- `keywords` match the job title **and** description. `OR` and brackets work; `NOT` is ignored.
- `location` is what you'd type in LinkedIn's location box: a country, a city or `European Union`.
- Seniority, remote and title filtering happen in job-poller's own `filters`, not on LinkedIn - LinkedIn's guest
  search ignores most of its filter parameters.
- Fewer, broader searches are better than many narrow ones: each search costs requests from the hourly budget.

### Optional settings

```yaml
linkedin:
  interval_minutes: 30         # minimum 30
  jitter_seconds: [15, 25]     # random gap before every request, minimum 15
  max_requests_per_hour: 30    # maximum 60, counted over a rolling hour
  fetch_details: true          # open each job that passes Stage 1 to read the full description (needed for scoring)
  max_details_per_cycle: 0     # 0 = no limit
  daily_catchup:               # once a day, look back 24 h with LinkedIn's "under 10 applicants" filter
    enabled: false
    at: "10:00"
    timezone: "Europe/Madrid"
```

### Turn it off

```bash
docker compose stop linkedin          # pause it
docker compose rm -sf linkedin        # remove the container
```

Then set `linkedin.enabled: false` in `config.yaml`. Plain `docker compose up -d` never starts the LinkedIn
container - only `--profile linkedin` does.

### What to expect

- **Blocks happen.** LinkedIn answers 429 even at a few requests per hour; it depends mostly on your IP's
  reputation, not on volume. Home connections do better than cloud servers. The log says when the next attempt
  is allowed, and the job boards keep running regardless.
- If the `linkedin` container logs `linkedin.enabled is false ... idle`, you started the profile but didn't turn
  the setting on (or the reverse: the `poller` log tells you when the setting is on but LinkedIn runs elsewhere).
- Links open as `linkedin.com/jobs/view/<id>`; LinkedIn may show a sign-in wall when you're logged out.

More background in [docs/linkedin.md](docs/linkedin.md).

## Everyday use

```bash
docker compose ps                     # are the containers up?
docker compose logs -f poller         # live log
docker compose logs --since 2h poller # recent log
docker compose stop                   # pause (data kept)
docker compose start                  # resume
docker compose down                   # stop and remove containers (data in ./data is kept)
docker compose --profile linkedin down  # same, including the LinkedIn container
```

Both containers use `restart: unless-stopped`, so they come back after a reboot as long as Docker starts.

## Commands

All commands run inside the poller image with `docker compose run --rm poller python -m jobpoller <command>`:

| Command | What it does |
|---|---|
| `run [--only ...] [--skip ...]` | The service loop. Tasks: `boards`, `discovery`, `linkedin`. The `poller` container runs `run --skip linkedin`, the `linkedin` container `run --only linkedin`. |
| `once` | One pass right now: load seed/config boards, poll all boards (and LinkedIn if enabled), score, notify, exit. Discovery is not included - use `discover`. |
| `test-notify` | Sends a test message to every configured channel. |
| `careers <url> <name>` | Shows which job board is behind a careers page, e.g. `careers https://aircall.io/careers/ "Aircall"`. |
| `discover` | Runs company -> board discovery now instead of waiting for the daily run. |
| `matches [--min N]` | Lists recent scored jobs (default: at or above your threshold). |
| `stats` | Counts: jobs seen, jobs passed Stage 1, scored, notified, active boards, companies. |
| `refresh-seed [--providers ...]` | Rebuilds `seed/boards.json` from Common Crawl for every region in `seed/regions.yaml`. |

Examples:

```bash
docker compose run --rm poller python -m jobpoller careers https://acme.com/careers "Acme"
docker compose run --rm poller python -m jobpoller matches --min 6
docker compose run --rm poller python -m jobpoller stats
```

### Adding a region

1. Add it to [seed/regions.yaml](seed/regions.yaml) - a name and the location words a job must contain:
   ```yaml
   netherlands:
     locations: [netherlands, amsterdam, rotterdam, utrecht, eindhoven]
   ```
2. Rebuild the seed list (takes a while; it reads the Common Crawl index and checks every board once):
   ```bash
   docker compose run --rm -v ./seed:/app/seed poller python -m jobpoller refresh-seed
   ```
3. Add the region to `regions:` in `config.yaml`, rebuild and restart:
   ```bash
   docker compose up -d --build
   ```

## Data, backups and reset

- All data lives in PostgreSQL, stored on your machine under `./data/pg`. It survives restarts and rebuilds.
- **Backup:**
  ```bash
  docker compose exec db pg_dump -U jobpoller --clean --if-exists jobpoller > backup.sql
  ```
- **Restore** (replaces the current data; stop the poller first with `docker compose stop poller`):
  ```bash
  docker compose exec -T db psql -U jobpoller jobpoller < backup.sql
  ```
- **Start over** (deletes every stored job and board state):
  ```bash
  docker compose down
  rm -rf data/          # on Linux the folder is owned by the postgres user: sudo rm -rf data/
  docker compose up -d
  ```

## Updating

```bash
git pull
docker compose up -d --build
```

Database migrations are applied automatically at start-up.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `env file .env not found` | Run `cp .env.example .env` (it can stay empty for a first try). |
| `config.yaml` errors at start-up, or a folder named `config.yaml` appeared | Create the file before `docker compose up`: `cp config.example.yaml config.yaml`. If Docker created a folder, delete it first. |
| No alerts after the first poll | Expected: the first poll is a baseline. Wait for new postings, or check `stats` to see jobs arriving. |
| Jobs found but nothing scored | `GEMINI_API_KEY` missing or quota used up - see the log. Pending jobs are scored once the key works. |
| Too many / too few alerts | Tune `filters` (Stage 1) and `scorer.criteria` / `alert_threshold` (Stage 2). `matches --min 5` shows what was just below the bar. |
| Telegram `chat not found` | Wrong chat id, or you haven't sent your bot a message yet ([docs/telegram.md](docs/telegram.md)). |
| A board shows as `dead` | It failed for about a day in a row (renamed, removed, moved provider). Find the new one with `careers`. |
| HTTP 429 warnings for a provider | Rate limited; the poller backs off and tries at the next slot. |
| Changed `POSTGRES_PASSWORD` and now the poller can't connect | The password is fixed when `./data/pg` is created. Change it back, or reset the data folder. |

## Development

Tests run in containers against a throwaway PostgreSQL (in memory, removed afterwards):

```bash
docker compose -f docker-compose.test.yml run --rm tests            # pytest
docker compose -f docker-compose.test.yml run --rm tests ruff check .
docker compose -f docker-compose.test.yml down -v
```

Without Docker (Python 3.12 and a PostgreSQL you run yourself):

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export DATABASE_URL=postgresql://user:pass@localhost:5432/jobpoller
export TEST_DATABASE_URL=$DATABASE_URL   # pipeline tests need a database
export JOBPOLLER_CONFIG=config.yaml
ruff check . && pytest -q
python -m jobpoller once
```

CI (GitHub Actions) runs ruff, pytest with a Postgres service, a Docker build and a gitleaks secret scan on every
push and pull request.

### Design notes

- One process with a small scheduler: board polls, daily discovery, optional LinkedIn cycles, then scoring. A
  failing task is logged and retried at its next slot; it never takes the others down.
- Idempotent: job keys are `<source>:<id>`, inserts are `ON CONFLICT DO NOTHING`, notifications are recorded per job
  and per company + title.
- Polite: honest `User-Agent`, per-provider pacing, `robots.txt` respected on careers pages, backs off on HTTP 429.

## Project layout

```
job-poller/
├── jobpoller/
│   ├── __main__.py         CLI (run, once, careers, matches, stats, ...)
│   ├── app.py              start-up sync and scheduler loop
│   ├── config.py           config.yaml / .env models (pydantic)
│   ├── boards_runner.py    board polling and discovery
│   ├── linkedin_runner.py  opt-in LinkedIn cycles
│   ├── filters.py          Stage 1 rules
│   ├── scorer.py           Stage 2 LLM scoring
│   ├── seed_refresh.py     rebuild the seed list from Common Crawl
│   ├── sources/            boards.py (7 providers), careers.py (board detection), linkedin.py
│   ├── notify/             console + Telegram
│   └── db/                 PostgreSQL store + SQL migrations
├── seed/                   boards.json (seed list), regions.yaml
├── tests/                  pytest (offline HTTP mocks + real Postgres pipeline test)
├── docs/                   architecture, telegram, linkedin
├── config.example.yaml
├── .env.example
├── Dockerfile              multi-stage: base / test / app (non-root)
├── docker-compose.yml      db + poller (+ linkedin with --profile linkedin)
└── docker-compose.test.yml tests with a throwaway Postgres
```

## Limitations

- Companies on their own career platforms (Workday, SuccessFactors, custom sites) are detected but not read yet.
- The seed list is a snapshot from a web-archive sample - not every company on each provider. Seed boards show the
  board slug as the company name.
- Gemini's free tier may use inputs to improve Google's products: only public postings and your profile text are
  sent.
- LinkedIn is opt-in, unofficial and often rate limited; LinkedIn's terms don't allow automated access - read
  [docs/linkedin.md](docs/linkedin.md).

## Security

Secrets belong only in `.env` (gitignored). See [SECURITY.md](SECURITY.md) for reporting issues.

## License

[MIT](LICENSE)
