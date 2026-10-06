# Marquee — Build Plan

A small, production-minded pipeline for live-event data, built in one day.

It pulls Los Angeles music events from the Ticketmaster Discovery API, keeps every response exactly as received, turns them into clean Postgres tables, checks its own work, and shows on a dashboard whether the data can be trusted right now.

---

## 1. Why this exists

A live-events trading desk runs on data that has to be complete, on time and trusted. This project is a one-day, honest version of that plumbing:

| What the project does | The principle it proves |
|---|---|
| Saves raw responses before touching them | Every number can be traced back to its source |
| Rebuilds clean tables from raw with zero API calls | Fixing a parsing bug never costs quota |
| Writes keyed on the source's ID | Re-running a job never creates duplicates |
| Splits searches into date windows | The API's 1,000-result cap can't silently drop data |
| Paces requests and retries only safe errors | Stays under rate limits without getting blocked |
| Logs every run and runs checks afterward | Silent failures become loud |
| Shows a freshness badge | Nobody has to guess whether the data is current |

### Non-goals (deliberately out of scope)

- **No resale prices.** Resale marketplaces have no public API. `priceRanges` from Ticketmaster are face-value ranges, present on some events only.
- **No scraping.** Official API only.
- **No models, no user accounts, no fancy frontend.**
- **Not commercial.** Data is cached only as long as the provider's terms allow (see §8).

---

## 2. Architecture

**Analogy:** a restaurant kitchen. Deliveries arrive (the API). The receiving log keeps every box exactly as delivered (raw storage). Prep cooks wash and portion it (clean tables). The pass checks every plate before it leaves (checks). The board out front shows what's fresh (the dashboard).

```
 Ticketmaster Discovery API
          │   paced at 4 req/s · safe retries · daily-budget guard
          ▼
   ┌──────────────┐     ┌────────────────────┐
   │  ingest run  │────►│  raw_responses     │  JSON exactly as received
   │  (one lock)  │     └─────────┬──────────┘
   └──────┬───────┘               │  transform: a pure function, re-runnable
          │                       ▼
          │      venues · attractions · events · event_attractions
          │      price_range_snapshots (append-only, change-only)
          │                       │
          ▼                       ▼
   ingest_runs ◄──────── checks ──► check_results
          │
          ▼
   Streamlit dashboard: freshness badge · run history · checks · events
```

### One run, step by step

1. Take a Postgres advisory lock, so two runs can never overlap. If the lock is held, exit quietly.
2. Open an `ingest_runs` row with status `running`.
3. **Plan windows:** split the next 90 days into date windows small enough that each has 1,000 results or fewer (§4).
4. For each window, fetch every page. **Each page is one transaction:** save raw → transform → upsert. A page either fully lands or not at all, so a crash halfway is always safe to re-run.
5. Run the checks (§5) and write `check_results`.
6. Prune raw responses older than the retention window (§8).
7. Close the run: `succeeded`, `partial` (budget guard stopped it early), or `failed` (with the error).

### Design rules

1. **Raw first, clean second.** The transform reads only from `raw_responses`, never from the API, so `marquee rebuild` recreates every clean table without spending a single call.
2. **Our own IDs.** Clean tables use our own primary keys, plus a `(source, source_id)` unique pair. A second marketplace later becomes new rows, not a redesign.
3. **Re-running never duplicates.** Every write is `INSERT … ON CONFLICT (source, source_id) DO UPDATE`.
4. **Silence becomes loud.** Every window records what the API said exists (`page.totalElements`) next to what we actually received.
5. **Boring tools.** Python, Postgres and a scheduler. Upgrade only when a written trigger is hit (decision record 003).

---

## 3. Data model

`migrations/001_init.sql`:

```sql
CREATE TABLE ingest_runs (
  run_id          bigserial PRIMARY KEY,
  source          text        NOT NULL,                  -- 'ticketmaster'
  started_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz,
  status          text        NOT NULL DEFAULT 'running', -- running | succeeded | partial | failed
  windows         int,
  api_calls       int         NOT NULL DEFAULT 0,
  reported_total  int,        -- sum of page.totalElements across windows
  fetched_total   int,        -- events received across windows (boundary overlaps included)
  unique_events   int,        -- distinct events after dedupe
  budget_left     int,        -- last Rate-Limit-Available seen
  error           text
);

CREATE TABLE raw_responses (
  raw_id       bigserial PRIMARY KEY,
  run_id       bigint      NOT NULL REFERENCES ingest_runs,
  fetched_at   timestamptz NOT NULL DEFAULT now(),
  endpoint     text        NOT NULL,
  params       jsonb       NOT NULL,   -- NEVER contains the API key
  http_status  int         NOT NULL,
  body         jsonb       NOT NULL
);
CREATE INDEX ON raw_responses (fetched_at);

CREATE TABLE venues (
  venue_id    bigserial PRIMARY KEY,
  source      text NOT NULL,
  source_id   text NOT NULL,
  name        text NOT NULL,
  city        text,
  state       text,
  timezone    text,
  latitude    double precision,
  longitude   double precision,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, source_id)
);

CREATE TABLE attractions (              -- artists, bands, shows
  attraction_id bigserial PRIMARY KEY,
  source        text NOT NULL,
  source_id     text NOT NULL,
  name          text NOT NULL,
  segment       text,
  genre         text,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, source_id)
);

CREATE TABLE events (
  event_id          bigserial PRIMARY KEY,
  source            text NOT NULL,
  source_id         text NOT NULL,
  name              text NOT NULL,
  venue_id          bigint REFERENCES venues,
  local_date        date,
  local_time        time,               -- null when time is TBA
  starts_at         timestamptz,        -- null when date or time is TBA/TBD
  status            text,               -- onsale | offsale | canceled | postponed | rescheduled
  segment           text,
  genre             text,
  public_sale_start timestamptz,
  url               text,
  first_seen_run    bigint REFERENCES ingest_runs,
  last_seen_run     bigint REFERENCES ingest_runs,
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, source_id)
);
CREATE INDEX ON events (local_date);

CREATE TABLE event_attractions (
  event_id      bigint   NOT NULL REFERENCES events ON DELETE CASCADE,
  attraction_id bigint   NOT NULL REFERENCES attractions,
  position      smallint NOT NULL,      -- 0 = headliner (first listed)
  PRIMARY KEY (event_id, attraction_id)
);

-- Append-only. A row is written only when min or max changed since the last row.
CREATE TABLE price_range_snapshots (
  event_id    bigint        NOT NULL REFERENCES events,
  run_id      bigint        NOT NULL REFERENCES ingest_runs,
  captured_at timestamptz   NOT NULL,
  price_type  text          NOT NULL,   -- coalesce missing type to 'unknown'
  currency    text,
  min_price   numeric(10,2),
  max_price   numeric(10,2),
  PRIMARY KEY (event_id, run_id, price_type)
);

CREATE TABLE check_results (
  run_id     bigint  NOT NULL REFERENCES ingest_runs,
  check_name text    NOT NULL,
  passed     boolean NOT NULL,
  observed   numeric,
  expected   numeric,
  detail     text,
  PRIMARY KEY (run_id, check_name)
);
```

`first_seen_run` / `last_seen_run` let you answer "when did this show appear?" and "which shows vanished?" without any extra tables.

---

## 4. The 1,000-result cap

**The rule (from the docs):** deep paging only reaches the 1,000th item (`size * page < 1000`). A search with 1,400 results quietly returns 1,000, with no error.

**Brute force first (ship it, run it, watch it break):** one search for LA music events over the next 90 days, paging until the end. Record `totalElements` against what was received. If LA is big enough, this is the real, observed bug for the README and the interview.

**The fix: adaptive window splitting.** Like splitting a stack of mail until each pile fits in one envelope:

1. Start with weekly windows.
2. For each window, fetch page 0 and read `page.totalElements`.
3. If it's over 1,000, split the window in half and repeat. The page-0 probe isn't wasted: its events are saved like any other page.
4. Stop splitting at a minimum of 1 day. If a single day is still over the cap, record a failed check (`window_over_cap`) instead of silently losing data.

**Boundaries:** windows share edges (one window's end is the next one's start). An event exactly on a boundary may come back twice, which is harmless because upserts dedupe. Gaps would lose data; overlaps can't.

**Two budgets, both with real numbers from Phase 0:**

1. **API calls.** Calls per run ≈ windows + extra pages. Scheduled runs must stay under about **2,500 calls a day**, half the 5,000 quota, leaving room for manual runs.
2. **Storage.** The project runs on Neon's free plan, which allows **1 GB per project** and suspends the database (it never charges) if that's exceeded. Raw JSON is the big consumer: raw MB per run × runs per day × retention days must stay under **500 MB**, leaving half for clean tables and headroom.

If either budget doesn't fit hourly runs, lower the frequency (every 2 or 3 hours) or the retention, and record the math in decision record 003. Running out of storage is a silent failure too, so the dashboard shows database size against the 1 GB limit.

---

## 5. Checks (after every run)

| Check | Rule | Catches |
|---|---|---|
| `fetched_vs_reported` | Per window, received equals `totalElements`, within a small tolerance (totals can shift while paging) | Paging bugs, the cap, dropped pages |
| `volume_vs_baseline` | `unique_events` ≥ 70% of the median of the last 7 successful runs (skipped until 3 runs exist) | A source silently returning less |
| `window_over_cap` | No window ended over 1,000 after splitting | Data we know we couldn't fetch |
| `price_sanity` | `0 < min_price ≤ max_price` wherever present | Units or parsing bugs |
| `events_without_venue` | Count only; a warning, not a failure | Upstream data gaps |

**Freshness** is computed by the dashboard, not stored: age since the last `succeeded` run. Green under 90 minutes, yellow under 3 hours, red beyond that (tuned to an hourly schedule).

---

## 6. HTTP client rules (`tm_client.py`)

- **Pace:** at least 250 ms between requests (4 per second; the limit is 5).
- **Retry only safe failures:** timeouts, connection errors, 429 and 5xx. Use exponential backoff with jitter, honor `Retry-After` if present, and give up after 5 tries.
- **Fail fast:** 400, 401 and 403 mean it's our bug or our key. Retrying only burns quota.
- **Budget guard:** read `Rate-Limit-Available`. If it falls below a reserve (default 200), stop cleanly and mark the run `partial`.
- **Never leak the key:** strip `apikey` from saved params, logs and exception messages. Test this.
- **Testable:** inject the HTTP transport (`httpx.MockTransport`), the clock and the sleep function. No real network calls in unit tests.

---

## 7. Project layout

```
marquee/
├── README.md
├── CLAUDE.md
├── pyproject.toml            # deps, ruff and pytest config
├── docker-compose.yml        # local Postgres 16 for tests
├── .env.example              # TM_API_KEY, DATABASE_URL, TEST_DATABASE_URL, settings
├── migrations/001_init.sql
├── src/marquee/
│   ├── __main__.py           # CLI: migrate | ingest | rebuild | checks | prune
│   ├── config.py             # settings from environment
│   ├── db.py                 # connection, migrations, advisory lock
│   ├── tm_client.py          # HTTP: pacing, retries, budget guard
│   ├── windows.py            # window planning and splitting (pure)
│   ├── transform.py          # raw JSON -> rows (pure)
│   ├── load.py               # upserts, change-only price snapshots
│   ├── ingest.py             # orchestrates one run
│   ├── checks.py             # check rules (pure where possible)
│   └── queries.py            # read queries for the dashboard
├── dashboard/app.py          # Streamlit
├── docs/
│   ├── PLAN.md
│   ├── api-notes.md          # what we actually observed from the API
│   ├── STATUS.md             # status notes, newest first
│   └── decisions/001-…md     # decision records
├── tests/
│   ├── fixtures/             # SYNTHETIC, shaped like real responses
│   └── test_*.py
├── local/                    # gitignored: real sample responses
└── .github/workflows/
    ├── ci.yml                # ruff + pytest (Postgres service container)
    └── ingest.yml            # hourly scheduled ingest
```

**Dependencies:** `httpx`, `psycopg[binary]`, `python-dotenv`, `streamlit`, plus dev-only `pytest` and `ruff`. Nothing else without a reason.

---

## 8. Data handling and the provider's terms

Ticketmaster's API terms don't allow caching or storing event content beyond reasonable periods needed to provide the service. So:

- **Raw retention:** `prune` deletes raw responses older than `RAW_RETENTION_DAYS` at the end of every run. The default is 3 days; set the final value from the storage budget in §4 and never above 14.
- **Nothing real in git:** real sample responses live in `local/` (gitignored). Test fixtures are synthetic: same shape, invented values.
- **Personal and non-commercial.** If the dashboard is hosted, it stays private.
- **Note it in the README.** This is a feature, not a footnote: the pipeline respects its source's rules by design.

---

## 9. Phases (about 11 hours)

Every phase starts with a plain-English plan and ends with its **done check run for real**.

| # | Phase | Time | Done when |
|---|---|---|---|
| 0 | **Probe the API.** Get the key and call endpoints by hand. Confirm the LA filter (try `dmaId=324` and check venue cities; otherwise `city=Los Angeles&stateCode=CA`), the `startDateTime` format (`YYYY-MM-DDTHH:mm:ssZ`, no milliseconds), the max `size`, `totalElements` for 90 days of LA music, the rate-limit headers, and the size in KB of a full page. Save one real response to `local/`. | 45 min | `docs/api-notes.md` lists auth, paging, limits, IDs, errors and page size, with **observed** values, plus both budgets from §4 worked out. No pipeline code yet. |
| 1 | **Skeleton.** `pyproject`, config, `docker-compose`, migration runner, `001_init.sql`, CI. | 1 h | `python -m marquee migrate` runs twice without error; CI is green. |
| 2 | **HTTP client.** | 1.5 h | Tests pass: 429 then success; 401 fails with no retry; five 500s give up; pacing ≥ 250 ms; budget guard stops cleanly; the key never appears in params, logs or errors. |
| 3 | **Windows.** Brute force first: a single window, run for real, record what happens. Then adaptive splitting. | 1.5 h | Tests: windows cover the whole range with no gaps; splitting stops at 1 day; an over-cap day is reported. The real-run numbers are written in `STATUS.md`. |
| 4 | **Raw, transform, load.** | 2 h | Tests: transform handles a missing venue, missing attractions, missing `priceRanges` and a TBA time; loading twice gives identical counts; `rebuild` reproduces identical clean tables (row counts plus a checksum); a price snapshot is written only on change. |
| 5 | **Checks, run record, prune.** | 1 h | Each check has a passing and a failing test. A real run shows its checks in `check_results`. |
| 6 | **Dashboard.** | 1.5 h | It shows the freshness badge, the last 24 runs (calls, reported vs fetched, status), the checks panel, an upcoming-events table with filters (date, venue, status), events per week, recent price-range changes, and database size against the 1 GB limit. |
| 7 | **Schedule.** `ingest.yml` runs at minute 17 on the schedule the §4 budgets allow (hourly if both fit), with secrets `TM_API_KEY` and `DATABASE_URL` (Neon). | 45 min | Two scheduled runs have succeeded and appear on the dashboard, and measured calls and storage per run match the budget. |
| 8 | **Docs and rehearsal.** README, decision records, `STATUS.md`. | 1 h | A fresh clone, following only the README, reaches a working dashboard. The demo is rehearsed twice. |

### Cut line (if you're behind at hour 6)

Cut in this order: (1) price snapshots, (2) the scheduled workflow (run ingest manually a few times instead), (3) dashboard extras beyond the freshness badge, run history and checks.

**Never cut:** raw-first storage, idempotent upserts, window splitting, checks, rebuild, tests, the README.

---

## 10. Decision records to write

One page each: the problem, the options, the choice, why, and what would change my mind.

1. **001: Raw first.** The transform reads only from raw.
2. **002: Our own IDs plus (source, source_id).** Ready for a second marketplace.
3. **003: Boring stack.** A scheduler, Postgres and Streamlit, with written upgrade triggers: add an orchestrator when jobs depend on each other, partition by month when price queries pass a few seconds, move to a warehouse when Postgres strains.
4. **004: Adaptive window splitting** for the 1,000-result cap.

---

## 11. Numbers to capture

Write these in `docs/STATUS.md` from real runs, and carry the final values into the README. **Never estimate them.**

- `totalElements` for 90 days of LA music, and whether the brute-force version lost data (and how much)
- How many windows splitting needed, and API calls per run
- Duplicates after re-running the same ingest (target: 0)
- Time to rebuild every clean table from raw, and the API calls it used (target: 0)
- Number of scheduled runs, and any check that ever failed and why
- Any real surprise in the data (missing fields, odd statuses, TBA times)
