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

1. **Raw first, clean second.** The transform reads only from `raw_responses`, never from the API.
   - Raw is gzip of the exact bytes received, with a SHA-256 (decision 005).
   - `marquee rebuild` replays retained raw onto the clean tables with zero calls.
   - `marquee rebuild --verify` rebuilds into a throwaway schema and compares.
   - Neither touches `event_changes`. What can't be reproduced after raw is pruned is listed in decision 001.
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

**`migrations/002_raw_gzip_and_run_counters.sql`** (decided by measurement, decision 005):
- `raw_responses.body` (jsonb) is replaced by `body_gzip` (bytea, `STORAGE EXTERNAL`), `body_sha256` and `body_bytes`.
- `ingest_runs` gains `probe_calls`, `probe_events`, `undated_events` and `onsale_nulled` (jsonb counts by reason).
- `ingest_runs` gains `range_start` and `range_end` (migration `004`): the instants the run covered, written when the run opens. Runs from before `004` were backfilled from `started_at` with the same rule as `plan_range`.

`first_seen_run` / `last_seen_run` let you answer "when did this show appear?" and "which shows vanished?" without any extra tables. A show that goes TBA stays visible through the undated calls (§4), so it isn't mistaken for a vanished one.

**Change detection** is a pure function (`changes.py`). It takes the stored row and the freshly transformed row, and returns one change per tracked field that differs. A first sighting is not a change; new shows come from `first_seen_run`.

**New shows are judged against the previous good run's range.** Every LA midnight the 90-day window moves forward a day, so that day's shows are seen for the first time without having just been listed. A show first seen in run R is:
- **newly listed** if its start was inside the range of the last succeeded run before R, or if it's undated;
- **entered the 90-day window** if its start lay beyond that range's end. The dashboard shows these as a count, not as news.

With no succeeded run before R, R was the baseline, and nothing in it is news.

**Values as the API sends them (Phase 0, `docs/api-notes.md`):**
- Status is spelled `cancelled`.
- Venue `latitude` and `longitude` are strings.
- `sales.public.startDateTime` uses `1900-01-01T06:00:00Z` or `1900-01-01T18:00:00Z` as a placeholder on about 18% of events.
  - An onsale is kept only if it's plausible: on or before the event's start (or the end of its local day, or for an undated event its `initialStartDate`) and no more than 2 years before it.
  - An undated event with no original date accepts any past onsale.
  - Anything else becomes NULL and is counted by reason in `ingest_runs.onsale_nulled`.
- `dates.timezone` is often missing, while the venue's `timezone` never was.

---

## 4. The paging cap

**The rule.**

- **Docs:** deep paging only reaches the 1,000th item (`size * page < 1000`).
- **Observed in Phase 0:** at the maximum `size=200`, pages 0–5 are served, so **1,200 items are reachable**. Page 6 returns **HTTP 400** (`DIS1035`).
- So the cap fails loudly, but everything past item 1,200 is out of reach.

**Brute force first (ship it, run it, watch it break):** one search for LA music events over the next 90 days, paging until the end. Record `totalElements` against what was received. Phase 0 already saw it break with the final query: 1,276 reported, 1,200 reachable, 76 lost (`docs/STATUS.md`).

**The fix: adaptive window splitting.** Like splitting a stack of mail until each pile fits in one envelope:

**Windows are Los Angeles calendar days, sent to the API as UTC.**
- The range runs from now to the Los Angeles midnight 90 days out.
- Every other boundary is a local midnight, so a day is 23, 24 or 25 hours long across daylight-saving changes.
- The API includes both edges, so windows share edges: nothing falls in a gap, and an event exactly on an edge is fetched twice and deduped.
- Events with no specific time sit inside their own local day (`docs/api-notes.md` §7).

1. Start with weekly windows.
2. For each window, fetch page 0 and read `page.totalElements`.
3. If it's over 1,000, split the window in half and repeat. The 1,000 threshold is the documented limit, kept as a margin below the observed 1,200. The page-0 probe isn't wasted: its events are saved like any other page.
4. Splits fall on a Los Angeles midnight. Stop at a minimum of 1 local day. If a single day is still over the cap, fetch what the API allows and record a failed check (`window_over_cap`) instead of silently losing data.

**Counting.** A run's reported, fetched and unique numbers come from the **final** windows only.
- Page 0 of a window that was split (a probe) is kept, because its events get saved. But they reappear in the child windows, so probe calls and probe events are counted separately.
- That keeps "reported vs fetched" like with like, and duplicates never make it look as if we fetched more events than exist.
- The proof of completeness is that unique event IDs across all windows equal the API's reported total for the whole range at that moment.

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

**Measured in Phase 4** (decision 003):
- A run is 15 calls and **1.82 MB of raw on disk**.
- Hourly with 3-day retention is **131 MB** of raw, against the 500 MB budget, and 360 calls a day, against 2,500.
- So **hourly with 3-day retention fits**, and neither fallback is needed.

Running out of storage is a silent failure too, so the dashboard shows database size against the 1 GB limit.

---

## 5. Checks (after every complete run)

The rules are pure functions (`checks.py`) over a snapshot of the run's facts. Results go to `check_results` with a severity (migration `003`):
- **error:** the run's data can't be trusted as complete.
- **warning:** something upstream looks off.

A run that didn't complete (`partial` or `failed`) skips the checks; it's already a bad run.

**Tolerance.** Two counts may differ by **max(2 events, 1%)**. Totals drift by a few events an hour (1,276 to 1,282 in one morning), and a strict equality would turn the scheduled run red for no real reason. False alarms teach people to ignore alarms.

| Check | Severity | Rule | Catches |
|---|---|---|---|
| `fetched_vs_reported` | error | In every final window, events received equal `totalElements`, within tolerance | Paging bugs, the cap, dropped pages |
| `unique_vs_total` | error | Unique event IDs across the windows equal the API's total for the whole range, read right after, within tolerance. The exact difference is recorded. Costs 1 call a run. | Gaps between windows; anything the per-window check can't see |
| `window_over_cap` | error | No final window reports more than 1,000 | Data we know we couldn't fetch |
| `volume_vs_baseline` | error | `unique_events` ≥ 70% of the median of the last 7 succeeded runs (exact fractions; skipped until 3 exist) | A source silently returning less |
| `events_without_venue` | warning | None of this run's events lacks a venue | Upstream data gaps |
| `venues_outside_ca` | warning | Count and list venues whose state isn't CA | Out-of-area venues tagged DMA 324 (2 in Canada) |
| `implausible_onsales` | warning | Every nulled onsale is the known 1900 placeholder; any other reason is listed | A new kind of placeholder, or a rule misfiring |

**Alert path: how a failure reaches a person.**

1. `ingest` exits **non-zero for any bad run**: `partial` (budget or quota), `failed`, or an **error-level check failed**.
2. The scheduled GitHub Actions run therefore **turns red**.
3. GitHub notifies **the user who created the scheduled workflow**, or whoever later changed its cron or re-enabled it. Here that's the repo owner, who pushes it.
4. The notification arrives **by email and/or on the web**, according to that user's settings: **github.com/settings/notifications → System → Actions**. Choose **Email** (and/or **On GitHub**), then **Only notify for failed workflows**, then **Save**. Emails go to the account's default notifications email, set on the same page.

(GitHub docs: "Notifications for scheduled workflows are sent to the user who initially created the workflow. If a different user updates the cron syntax … subsequent notifications will be sent to that user instead." Re-enabling a disabled schedule also moves them to that user.) Warnings don't make the run red; they show on the dashboard. This path goes into the README in Phase 8.

**The scheduler's limits (GitHub Actions), and how each shows up.**

- **Delays.** GitHub says scheduled runs "can be delayed during periods of high loads", and that "high load times include the start of every hour". That's why the cron is minute 17. A late run shows as **LATE** on the dashboard (1.5 intervals), not as an error.
- **Disabled after 60 days.** "In a public repository, scheduled workflows are automatically disabled when no repository activity has occurred in 60 days." Marquee's repository is public. **A disabled schedule is silent**: no run means no red run and no email, so the only signal is the dashboard going **STALE**. Any commit counts as activity, and the workflow can be re-enabled from the Actions tab.
- **Never overlapping.** The workflow's concurrency group queues a new run behind one in progress and never cancels it; GitHub keeps at most one run waiting. The ingest advisory lock also guards against a manual run from a laptop.
- **Public logs.** On a public repository anyone can read the run logs. They hold the run summary and failed-check details (today, two venue names), never a secret: GitHub masks secret values, and the app never logs the key.

**Prune** runs at the end of every run:
- It deletes the raw of **whole runs** that started more than `RAW_RETENTION_DAYS` ago.
- It **always keeps the latest succeeded run's raw**, so a rebuild has one complete run even if the scheduler stopped.
- Run records and check results are kept.
- Manual use: `python -m marquee prune [--dry-run]`. Report a run's checks with `python -m marquee checks [--run N]`.

**Freshness** is computed by the dashboard, not stored: the age since the last `succeeded` run, measured against the schedule interval (a setting). Under 1.5 intervals it shows **Fresh** in the accent colour, under 3 intervals **LATE** in amber, beyond that **STALE** in red. The word always shows, so colour is never the only signal. For an hourly schedule that's 90 minutes and 3 hours.

---

## 6. HTTP client rules (`tm_client.py`)

- **Pace:** at least 250 ms between requests (4 per second; the limit is 5).
- **Retry only safe failures:**
  - Which failures: timeouts, connection errors, 429 throttles, 5xx, and a 200 whose body isn't JSON or has no `page` object. That last one is usually a passing glitch, and it must never reach the transform as data.
  - How: exponential backoff with jitter (1, 2, 4, 8 s, each times a random factor of 0.5–1), and give up after 5 attempts.
  - `Retry-After` is honoured up to 60 s. A longer wait gives up and leaves the work to the next scheduled run.
- **Two kinds of 429:**
  - **Daily quota gone** (`policies.ratelimit.QuotaViolation`): stop cleanly, like the budget guard. Retrying only burns attempts until the reset.
  - **Per-second throttle** (`policies.ratelimit.SpikeArrestViolation`): retry.
  - **No code we recognise:** stop if the remaining-budget estimate is 10 or less, otherwise retry.
  - The codes and their sources are in `docs/api-notes.md` §10.
- **Fail fast:** any other 4xx (400, 401, 403) means it's our bug or our key. Retrying only burns quota.
- **Budget guard:**
  - The estimate is the last `Rate-Limit-Available`, minus every call sent since. Phase 0 showed that 400s cost quota but carry no rate-limit headers.
  - Never send a call while the estimate is at or below the reserve (default 200). Stop cleanly and mark the run `partial`.
- **Timeouts:** named and set in one place: connect 5 s, read 30 s, write 5 s, pool 5 s. Not httpx's defaults.
- **Never leak the key:** strip `apikey` from saved params, logs and exception messages, and test it.
  - httpx logs every request URL at INFO, so the `httpx` and `httpcore` loggers are held at WARNING.
  - Ticketmaster's documented quota 429 repeats the key in its body, so error text is redacted.
  - httpx's own exceptions hold the URL, so none is ever chained to ours.
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
│   ├── __main__.py           # CLI: migrate | brute-force | fetch-windows | ingest | rebuild [--verify] | checks | prune
│   ├── config.py             # settings from environment
│   ├── db.py                 # connection, migrations, named advisory locks (migrate, ingest)
│   ├── tm_client.py          # HTTP: pacing, retries, budget guard
│   ├── windows.py            # Los Angeles-day windows and splitting, sent as UTC (pure)
│   ├── fetch.py              # paging a window to the end; adaptive splitting (via tm_client)
│   ├── transform.py          # raw JSON -> rows (pure)
│   ├── changes.py            # stored row vs new row -> event_changes (pure)
│   ├── load.py               # upserts, event_changes inserts
│   ├── ingest.py             # orchestrates one run (windows + 2 undated calls, page by page)
│   ├── rebuild.py            # replay raw onto live tables; --verify into a throwaway schema
│   ├── checks.py             # check rules (pure where possible)
│   ├── queries.py            # every dashboard SQL statement; read-only connections, 5 s timeout
│   └── present.py            # dashboard formatting: LA times, words for every problem state (pure)
├── dashboard/app.py          # Streamlit, layout only (SQL in queries.py, strings in present.py)
├── .streamlit/config.toml    # theme: one accent colour
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
| 4 | **Raw, transform, change detection, load.** | 2 h | Tests: transform handles a missing venue, missing attractions, a TBA date, a missing time (`noSpecificTime`), lat/long as strings and the 1900 onsale placeholder; loading twice gives identical counts; `rebuild --verify` (into a throwaway schema, compared on natural keys and content) finds no differences. Change detection, a pure function with tests first, writes one `event_changes` row per changed field (status, local date, local time, venue, public sale start), none when nothing changed, and none on a first sighting. On-disk raw size per run is measured and the schedule is chosen (§4). |
| 5 | **Checks, run record, prune.** | 1 h | Each check has a passing and a failing test. A real run shows its checks in `check_results`. |
| 6 | **Dashboard.** | 1.5 h | Read-only connections (tested: writes fail). Scope label and the completeness proof ("N reported · N received · N missing") up top. LA times. No colour without a word. It shows the freshness badge, the last 24 runs (calls, reported vs fetched, status), the checks panel, an upcoming-events table with filters (date, venue, status), events per week, changes since the last day (new shows via `first_seen_run`, newly listed apart from those that only entered the window, §3; postponed, cancelled and rescheduled shows; date moves), one small panel of public onsales in the next 7 days (Phase 0: 6 such events; the 1900-01-01 placeholder reads as no date), and database size against the 1 GB limit. |
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
