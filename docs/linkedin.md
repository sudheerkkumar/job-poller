# LinkedIn (opt-in)

job-poller can poll LinkedIn's **public guest job search** - the same pages anyone can open without logging in.
It is **off by default**.

## Read this before turning it on

- **LinkedIn's User Agreement does not allow automated access** to its site. Enabling this is your decision and
  your responsibility. The job boards (the default source) use the providers' official public APIs and have no
  such restriction.
- What job-poller does: guest pages only - **no login, no cookies kept, no browser automation**, an honest
  `User-Agent` (`job-poller/...`), and slow, fixed pacing.
- What it will not do: it does not retry around blocks, rotate IPs or disguise itself. On HTTP 429 (rate limited)
  or a redirect to a login page it stops, backs off (60, then 120 minutes) and widens the next search window so
  no time range is lost.

## Pacing floors (enforced in code, config can only make them slower)

| Setting | Floor |
|---|---|
| Gap between requests | ≥ 15 s (random 15-25 s by default) |
| Interval between cycles | ≥ 30 min |
| Requests per rolling hour | ≤ 60 |

## Turning it on

```yaml
linkedin:
  enabled: true
  searches:
    - { keywords: "senior (backend OR java OR kotlin)", location: "Spain" }
```

Notes from real use:
- Keywords match the title **and** the description; `OR` works, `NOT` is ignored. Seniority/remote filtering
  happens in job-poller's own filters.
- Blocks (429) happen even at a few requests per hour and are mostly about the IP's reputation, not volume.
  Datacenter/cloud IPs are blocked more often than home connections. Expect gaps; the job boards keep running.
