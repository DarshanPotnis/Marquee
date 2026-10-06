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
| Splits searches into date windows | The API's paging cap can't drop data |
| Paces requests and retries only safe errors | Stays under rate limits without getting blocked |
| Logs every run and runs checks afterward | Silent failures become loud |
| Shows a freshness badge | Nobody has to guess whether the data is current |

### Non-goals (deliberately out of scope)

- **No prices.** Resale marketplaces have no public API. Ticketmaster's face-value `priceRanges` was absent on all 1,200 events fetched in Phase 0, so no prices are stored.
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
          │      event_changes (append-only, one row per changed field)
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
4. For each window, fetch every page. Then make the two undated calls for TBA and TBD events (§4). **Each page is one transaction:** save raw → transform → detect changes against the stored rows → write `event_changes` → upsert. A page either fully lands or not at all, so a crash halfway is always safe to re-run.
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
  latitude    double precision,   -- arrives as a JSON string; parsed
  longitude   double precision,   -- arrives as a JSON string; parsed
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
  status            text,               -- as the API spells it: onsale | offsale | cancelled | postponed | rescheduled
  segment           text,
  genre             text,
  public_sale_start timestamptz,        -- null when missing or the 1900-01-01 placeholder
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

-- Append-only. One row per tracked field that differs between a re-fetched event and what we stored.
CREATE TABLE event_changes (
  event_id    bigint      NOT NULL REFERENCES events,
  run_id      bigint      NOT NULL REFERENCES ingest_runs,
  detected_at timestamptz NOT NULL,
  field       text        NOT NULL CHECK (field IN
                ('status', 'local_date', 'local_time', 'venue', 'public_sale_start')),
  old_value   text,                     -- null when the stored value was null
  new_value   text,                     -- null when the value went away (e.g. a show became TBA)
  PRIMARY KEY (event_id, run_id, field)
);
CREATE INDEX ON event_changes (detected_at);

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

`first_seen_run` / `last_seen_run` let you answer "when did this show appear?" and "which shows vanished?" without any extra tables. A show that goes TBA stays visible through the undated calls (§4), so it isn't mistaken for a vanished one.

**Change detection** is a pure function (`changes.py`). It takes the stored row and the freshly transformed row, and returns one change per tracked field that differs. A first sighting is not a change; new shows come from `first_seen_run`.

**Values as the API sends them (Phase 0, `docs/api-notes.md`):**
- Status is spelled `cancelled`.
- Venue `latitude` and `longitude` are strings.
- `sales.public.startDateTime` uses `1900-01-01T06:00:00Z` or `1900-01-01T18:00:00Z` as a placeholder on about 18% of events.
- `dates.timezone` is often missing, while the venue's `timezone` never was.

---

## 4. The paging cap

**The rule.**

- **Docs:** deep paging only reaches the 1,000th item (`size * page < 1000`).
- **Observed in Phase 0:** at the maximum `size=200`, pages 0–5 are served, so **1,200 items are reachable**. Page 6 returns **HTTP 400** (`DIS1035`).
- So the cap fails loudly, but everything past item 1,200 is out of reach.

**Brute force first (ship it, run it, watch it break):** one search for LA music events over the next 90 days, paging until the end. Record `totalElements` against what was received. Phase 0 already saw it break with the final query: 1,276 reported, 1,200 reachable, 76 lost (`docs/STATUS.md`).

**The fix: adaptive window splitting.** Like splitting a stack of mail until each pile fits in one envelope:

1. Start with weekly windows.
2. For each window, fetch page 0 and read `page.totalElements`.
3. If it's over 1,000, split the window in half and repeat. The 1,000 threshold is the documented limit, kept as a margin below the observed 1,200. The page-0 probe isn't wasted: its events are saved like any other page.
4. Stop splitting at a minimum of 1 day. If a single day is still over the cap, record a failed check (`window_over_cap`) instead of silently losing data.

**Boundaries:** windows share edges (one window's end is the next one's start). An event exactly on a boundary may come back twice, which is harmless because upserts dedupe. Gaps would lose data; overlaps can't.

**Undated events.** Events whose date is TBA or TBD never match a date-filtered search. Each run makes two extra undated calls: `includeTBA=only` and `includeTBD=only`. Setting both to `only` in one call returns nothing. Phase 0 found 19 such events, 18 of them postponed.

**Two budgets, both with real numbers from Phase 0:**

1. **API calls.**
   - Calls per run = windows + extra pages + 2 undated calls. Phase 0 measured **15**: 13 one-page windows plus 2.
   - Scheduled runs must stay under about **2,500 calls a day**, half the 5,000 quota, leaving room for manual runs. Hourly is 360.
2. **Storage.**
   - The project runs on Neon's free plan, which allows **1 GB per project** and suspends the database (it never charges) if that's exceeded.
   - Raw JSON is the big consumer: raw MB per run × runs per day × retention days must stay under **500 MB**, leaving half for clean tables and headroom.
   - Phase 0 measured **14.46 MB of JSON text per run**. Hourly with 3-day retention is 1,041 MB of text.
   - Postgres compresses large values, so Phase 4 measures the real on-disk size per run with `pg_total_relation_size('raw_responses')`.

If hourly runs with 3-day retention don't fit on disk, the options in order are:

1. Run every 3 hours.
2. Store a raw page only when its content hash changed.

Record the math in decision record 003. Running out of storage is a silent failure too, so the dashboard shows database size against the 1 GB limit.

---

## 5. Checks (after every run)

| Check | Rule | Catches |
|---|---|---|
| `fetched_vs_reported` | Per window, received equals `totalElements`, within a small tolerance (totals can shift while paging) | Paging bugs, the cap, dropped pages |
| `volume_vs_baseline` | `unique_events` ≥ 70% of the median of the last 7 successful runs (skipped until 3 runs exist) | A source silently returning less |
| `window_over_cap` | No window ended over 1,000 after splitting | Data we know we couldn't fetch |
| `events_without_venue` | Count only; a warning, not a failure | Upstream data gaps |
| `venues_outside_ca` | Count and list venues whose state isn't CA; a warning, not a failure | Out-of-area venues tagged DMA 324 (Phase 0: 2 venues in Canada, 3 events) |

**Freshness** is computed by the dashboard, not stored: the age since the last `succeeded` run, measured against the schedule interval (a setting). Green under 1.5 intervals, yellow under 3 intervals, red beyond that. For an hourly schedule that's 90 minutes and 3 hours.

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
├── pyproject.toml            # deps, ruff, mypy and pytest config (uv; uv.lock committed)
├── docker-compose.yml        # local Postgres 18 for tests (same major version as Neon)
├── .env.example              # TM_API_KEY, MARQUEE_DATABASE_URL, MARQUEE_TEST_DATABASE_URL, settings
├── migrations/001_init.sql
├── src/marquee/
│   ├── __main__.py           # CLI: migrate | ingest | rebuild | checks | prune
│   ├── config.py             # settings from environment
│   ├── db.py                 # connection, migrations, named advisory locks (migrate, ingest)
│   ├── tm_client.py          # HTTP: pacing, retries, budget guard
│   ├── windows.py            # window planning and splitting (pure)
│   ├── transform.py          # raw JSON -> rows (pure)
│   ├── changes.py            # stored row vs new row -> event_changes (pure)
│   ├── load.py               # upserts, event_changes inserts
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
    ├── ci.yml                # ruff + mypy --strict + pytest (Postgres 18 service container)
    └── ingest.yml            # hourly scheduled ingest
```

**Dependencies:** `httpx`, `psycopg[binary]`, `python-dotenv`, `streamlit`, plus dev-only `pytest`, `ruff` and `mypy` (strict, on `src/marquee`). Build backend `uv_build`. CI actions are pinned to the commit of a specific release. Nothing else without a reason.

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
| 1 | **Skeleton.** `pyproject`, config, `docker-compose`, migration runner, `001_init.sql`, CI. | 1 h | `python -m marquee migrate` runs twice without error; `ruff`, `mypy --strict` and `pytest` pass; CI is green. |
| 2 | **HTTP client.** | 1.5 h | Tests pass: 429 then success; 401 fails with no retry; five 500s give up; pacing ≥ 250 ms; budget guard stops cleanly; the key never appears in params, logs or errors. |
| 3 | **Windows.** Brute force first: a single window, run for real, record what happens. Then adaptive splitting. | 1.5 h | Tests: windows cover the whole range with no gaps; splitting stops at 1 day; an over-cap day is reported. The real-run numbers are written in `STATUS.md`. |
| 4 | **Raw, transform, change detection, load.** | 2 h | Tests: transform handles a missing venue, missing attractions, a TBA date, a missing time (`noSpecificTime`), lat/long as strings and the 1900 onsale placeholder; loading twice gives identical counts; `rebuild` reproduces identical clean tables (row counts plus a checksum). Change detection, a pure function with tests first, writes one `event_changes` row per changed field (status, local date, local time, venue, public sale start), none when nothing changed, and none on a first sighting. On-disk raw size per run is measured and the schedule is chosen (§4). |
| 5 | **Checks, run record, prune.** | 1 h | Each check has a passing and a failing test. A real run shows its checks in `check_results`. |
| 6 | **Dashboard.** | 1.5 h | It shows the freshness badge, the last 24 runs (calls, reported vs fetched, status), the checks panel, an upcoming-events table with filters (date, venue, status), events per week, changes since the last day (new shows via `first_seen_run`; postponed, cancelled and rescheduled shows; date moves), one small panel of public onsales in the next 7 days (Phase 0: 6 such events; the 1900-01-01 placeholder reads as no date), and database size against the 1 GB limit. |
| 7 | **Schedule.** `ingest.yml` runs at minute 17 on the schedule the §4 budgets allow (hourly if both fit), with secrets `TM_API_KEY` and `MARQUEE_DATABASE_URL` (Neon). | 45 min | Two scheduled runs have succeeded and appear on the dashboard, and measured calls and storage per run match the budget. |
| 8 | **Docs and rehearsal.** README, decision records, `STATUS.md`. | 1 h | A fresh clone, following only the README, reaches a working dashboard. The demo is rehearsed twice. |

### Cut line (if you're behind at hour 6)

Cut in this order: (1) `event_changes` and the onsale panel, (2) the scheduled workflow (run ingest manually a few times instead), (3) dashboard extras beyond the freshness badge, run history and checks.

**Never cut:** raw-first storage, idempotent upserts, window splitting, checks, rebuild, tests, the README.

---

## 10. Decision records to write

One page each: the problem, the options, the choice, why, and what would change my mind.

1. **001: Raw first.** The transform reads only from raw.
2. **002: Our own IDs plus (source, source_id).** Ready for a second marketplace.
3. **003: Boring stack.** A scheduler, Postgres and Streamlit, with written upgrade triggers: add an orchestrator when jobs depend on each other, partition by month when `event_changes` or raw queries pass a few seconds, move to a warehouse when Postgres strains.
4. **004: Adaptive window splitting** for the paging cap.

---

## 11. Numbers to capture

Write these in `docs/STATUS.md` from real runs, and carry the final values into the README. **Never estimate them.**

- `totalElements` for 90 days of LA music, and whether the brute-force version lost data (and how much)
- How many windows splitting needed, and API calls per run
- Duplicates after re-running the same ingest (target: 0)
- Time to rebuild every clean table from raw, and the API calls it used (target: 0)
- Number of scheduled runs, and any check that ever failed and why
- `event_changes` rows across scheduled runs, by field, and undated (TBA/TBD) events captured per run
- On-disk raw size per run (`pg_total_relation_size`) and the schedule it allowed
- Any real surprise in the data (missing fields, odd statuses, TBA times)
