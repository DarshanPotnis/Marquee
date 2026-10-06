# Status

Newest first. Every number here comes from a real run, never an estimate. Full detail is in `docs/api-notes.md`.

---

## 2026-10-06: Phase 6, dashboard

### Done check on production

| Check | Result |
|---|---|
| **Read-only, proven on Neon** | Through a dashboard connection, `CREATE TABLE` was refused with `ReadOnlySqlTransaction` (inside a transaction rolled back regardless). `statement_timeout` = 5s. |
| Render (Streamlit `AppTest`, headless, real data) | **0 exceptions**, 3.4 s |

| Panel | What it showed |
|---|---|
| Scope | Ticketmaster LA market (DMA 324) · Music · next 90 days · face-value data, no resale prices |
| Freshness | **Fresh** · last good run 53 minutes ago (Oct 6, 1:19 PM PDT) |
| **Completeness (headline)** | **1,289 reported · 1,289 received · 0 missing**; +19 undated (TBA/TBD) · run 4 |
| Checks | 0 failed · 1 **WARNING**: `venues_outside_ca`, the 2 Canadian venues |
| New shows | **5 real new shows**, first seen in runs 2–4 (run 1 is the baseline, not news) |
| Changes | "No changes in the last 24 hours." (runs 2–4 recorded 0) |
| Public onsales, next 7 days | 9 |
| Upcoming (default next 14 days) | 339 events |
| Last 24 runs | 4 |
| Database | 17.9 MB of 1,024 MB (1.7%) |

### Tests

- **320 pass** (about 20 s on Docker). ruff and mypy strict are clean, and mypy now covers `dashboard/app.py` too (the package ships a `py.typed` marker).
- **Read-only:** `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE` and `CREATE TABLE` each fail through a dashboard connection.
- **Empty database:** every query returns an empty answer, and the app shows every empty-state message.
- **Changes** read show / venue / event date / old → new, newest first, with venue IDs shown as names.
- **Words for problems:** `WARNING`, `PARTIAL` and `FAILED` appear in words in both the headline and the tables.

### Notes

- **mypy reported false errors from a stale cache** after `py.typed` was added; clearing `.mypy_cache` fixed it. The cache folder is now gitignored.
- **Neon console storage figure: skipped by decision.** The SQL measurement (`pg_database_size`, per-table sizes) is the record.

---

## 2026-10-06: Phase 5, checks, run record, prune

### Done check on production

| Step | Result |
|---|---|
| `migrate` | `applied 1 (003_check_severity_and_prune.sql), already applied 2` |
| `ingest` run 4 | **succeeded, exit 0**: 13 windows, 16 calls (13 + the whole-range total + 2 undated); reported = fetched = 1,289; unique 1,308 (19 undated); 0 changes; **checks: 7 run, 0 failed, 1 warning**; pruned 0 |
| `fetched_vs_reported` | passed: 13 windows within tolerance, largest gap 0 |
| `unique_vs_total` | passed: unique 1,289 vs total 1,289, difference 0 (tolerance 12) |
| `volume_vs_baseline` | passed, **computed for real**: 1,308 against the median 1,303 of runs 1–3 (floor 912.1) |
| `window_over_cap` | passed: no window over 1,000 |
| `events_without_venue` | passed: 0 |
| `implausible_onsales` | passed: `placeholder_1900` 243 (known) |
| `venues_outside_ca` | **warning**: Studio Theatre (Perth, ON); The Olaus Ice Palace (Rossland, BC) |
| `checks` report | prints all 7, ordered by severity |
| `prune --dry-run` | cutoff 2026-10-03T20:19:31Z (3 days); 0 raw to delete; always keeping run 4 (latest succeeded) |

### Tests

- **256 pass** locally (about 12 s on Docker); ruff and mypy are clean.
- **Every check** has a passing and a failing test.
- **Every error check also fails end to end** through the fake API:
  - over-reported windows,
  - a total above what the windows return,
  - a 1,050-event day,
  - a sharp drop against seeded prior runs.
- **Breakage test:** each change below made its tests fail.
  - `ingest` ignoring partial and failed runs in its exit code: 2 fail.
  - `prune` not keeping the latest succeeded run: 1 fails.
  - Tolerance set to 0: 8 fail.

### Surprises

1. **Ingest's own prune affected the prune tests' setup.** Building aged runs through `ingest` pruned the oldest run before the test's own prune ran. That's correct behaviour; the helper now uses a 14-day retention, so only the pruning under test happens.
2. **Floating point:** 0.7 × 1,300 is 909.999… in floats, so a run at exactly 70% of the median could have been judged wrongly. The floor now uses exact fractions; the boundary test caught it.
3. **The whole-range total isn't saved as raw.** It's a count used by a check, not data. A run is 16 calls but 15 raw pages.

**The Neon console storage figure is still to record.** Both messages had a blank (`___ MB`).

---

## 2026-10-06: Phase 4 follow-ups

| Item | Result |
|---|---|
| **Password rotated** | You reset `neondb_owner` on both branches. Production connects with the new `MARQUEE_DATABASE_URL`, checked without printing it. `migrate`: `applied 0, already applied 2`. |
| **Local tests on Docker** | `MARQUEE_TEST_DATABASE_URL` in `.env` now points at the local container (port 55432). The full suite takes **about 7 s** locally, against 12 min 24 s on Neon. CI is unchanged. |
| **Undated onsale rule** (tests first, 7 new tests, **215 passing**) | An undated event's onsale is checked against `dates.initialStartDate` when present. Otherwise any past onsale is accepted. Placeholders, unparseable values and anything more than 2 years in the future are still nulled and counted. |
| Proof on production | `rebuild` (the repair tool after a transform fix, which records no changes) → `rebuild --verify` with 0 differences → `ingest` run 3. The postponed show's **2024-07-26T17:00:00Z** onsale is **kept**; `too_early` went from 1 to **0**; run 3 had **0 changes**, reported 1,285, unique 1,304 (19 undated). |
| **Schedule** | **Hourly with 3-day raw retention: confirmed** (decision 003). |
| **Neon compute estimate** (hourly) | About 22 s of work plus the 5-minute idle tail is about **5.4 min awake per run**, about 64 h of wall time a month, so **16 CU-hours at 0.25 CU, up to 32 at 0.5 CU**, against the free plan's 100 (decision 003). |
| **Neon console storage figure** | **Still to record:** the value in your message was blank (`___`). |

Why `rebuild` came before `ingest`: an ingest straight after the fix would have recorded a "public_sale_start changed" row for that show, caused by our fix and not by Ticketmaster. Rebuild applies a transform fix to existing rows without inventing history.

---

## 2026-10-06: Phase 4, raw, transform, change detection, load

### Done check on production (Neon main)

| Step | Result |
|---|---|
| `migrate` | `applied 1 (002_raw_gzip_and_run_counters.sql), already applied 1` |
| `ingest` run 1 | 13 windows (0 split), 15 calls; reported 1,284, fetched 1,284; **unique 1,303** (19 undated); 0 changes; 22 s |
| `ingest` run 2, straight after | Identical results; **0 changes**. Clean tables identical to after run 1, so **0 duplicate rows**: venues 128, attractions 1,893, events 1,303, event_attractions 2,296, event_changes 0. Raw went from 15 to 30 pages and runs from 1 to 2. |
| `rebuild --verify` | Replayed 30 raw pages from 2 runs into a throwaway schema: **0 differences** in venues, attractions, events and event_attractions. Live tables unchanged, no scratch schema left, 0 API calls, 23 s. |
| Onsale plausibility | Nulled **`placeholder_1900` 239** and **`too_early` 1** per run (see surprise 2) |

### Storage (production, after 2 runs)

| Table | Size |
|---|---|
| `raw_responses` | 3,736 KB, so **1.82 MB per run** (14.60 MB as received; 1.69 MB of gzip plus storage overhead) |
| `events` | 968 KB |
| `attractions` | 448 KB |
| `event_attractions` | 424 KB |
| `venues` | 88 KB |
| All tables | 5.63 MB |
| Database (`pg_database_size`) | 13.48 MB, of which about 8 MB is Postgres's own catalog |
| Neon limit (`neon.max_cluster_size`) | 1 GB |
| Neon's console storage figure | *not visible in SQL; to be read from the console* |

**Proposed: hourly runs, 3-day raw retention** (decision 003).
- 360 calls a day, against the 2,500 budget.
- 131 MB of raw, against the 500 MB budget.
- 7 days would be 306 MB. Every 3 hours would be 44 MB at 3 days.

### Raw storage measurement (dev branch, one real run, 15 responses)

| Format | On disk | Read and parse | Exact? |
|---|---|---|---|
| `jsonb` | 6.32 MB | 1.25 s | 0 of 15 |
| `jsonb` with lz4 | 3.58 MB | 1.01 s | 0 of 15 |
| **gzip `bytea`** | **1.84 MB** | 0.44 s | 15 of 15 |

Chosen: gzip `bytea` (decision 005, migration `002`).

### Tests

- **208 passed** (`ruff` and `mypy --strict` clean).
- **Rules covered:** a first sighting isn't a change; an undated event getting a date is a change; an unchanged rerun gives 0 changes; a repeat change within one run updates that run's row (or removes it if the field ends where it started).
- **Ingest:** quota stop gives `partial`; crash gives `failed`, then a clean rerun; a held lock exits quietly.
- **Rebuild:** `--verify` reports a tampered row and leaves it alone; after pruning, rows without raw are left alone and reported.
- **Run time:** against Neon, `test_ingest.py` takes about 4 minutes and `test_rebuild.py` about 7.5 minutes, because of network round trips. CI (local Postgres) runs the same suite in under a second.

### Surprises

1. **A failing test printed the dev-branch database password.**
   - pytest shows fixture values when a test fails, and the `Schema` fixture's URL held the password.
   - **Fixed** (`68c59e9`): `repr=False`, all test connections go through the scrubbing `db.connect`, and a check proves a failing test no longer prints the URL (it fails without the fix).
   - **Action for you:** rotate the `neondb_owner` password. Neon branches usually inherit role passwords, so production may share it.
2. **The undated-event onsale rule nulled a real date.**
   - A postponed TBA show's onsale was 2024-07-26, more than 2 years before the run, so `too_early` nulled it.
   - It was genuine: the show went on sale in 2024 and was later postponed. It has no `initialStartDate`.
   - Among the 19 undated events, onsale years are 1900, 2024, 2025 and 2026. 9 of the 19 carry `initialStartDate`.
   - **The rule needs your decision.**
3. **The 1900 placeholder count rose,** from 235 (Phase 0) to 239.
4. **No real changes appeared between the two runs** (seconds apart), as expected. Change detection is proven by tests, not yet by live data.

---

## 2026-10-06: Phase 3 follow-up, splitting proven on the real API

`fetch-windows` gained `--split-threshold N` (default 1,000). It ran once for real at **N = 100**, so that ordinary weeks had to split.

```
fetch-windows: 2026-10-06T18:12:54Z .. 2027-01-04T08:00:00Z, 13 weekly windows planned, split threshold 100
fetch-windows: 22 final windows, 9 split (9 probe calls, 1175 probe events)
fetch-windows: final windows: reported 1282, fetched 1282, unique 1282
fetch-windows: whole range reported 1282 now; unique == reported: yes
fetch-windows: 32 calls (31 for windows + 1 whole-range check); over-cap days: none
```

- **The proof holds with real splitting:** unique IDs (1,282) equal the reported total (1,282).
- **The probe pages were counted separately.** They returned 1,175 events, all of which reappeared in the child windows. Had they been added to "fetched", the run would have seemed to fetch 2,457 events when only 1,282 exist.
- **No event fell exactly on one of the 22 local-midnight edges**, so fetched still equalled unique.
- **Cost:** 32 calls instead of 14. That's why the production threshold stays at 1,000.

---

## 2026-10-06: Phase 3, windows

### Before and after: same query, same range, run back to back

| | |
|---|---|
| Query | Ticketmaster LA market (DMA 324), Music segment |
| Range | `--start 2026-10-06T18:01:23Z` to `2027-01-04T08:00:00Z` |
| Timing | the two commands ran 7 s apart |

| | **Before:** `brute-force` (11:01:29 PDT) | **After:** `fetch-windows` (11:01:36 PDT) |
|---|---|---|
| API's reported total at that moment | **1,282** | **1,282** |
| How | one search, paged to the end | 13 weekly windows; 0 split, so 0 probe calls and 0 probe events |
| Fetched | **1,200**; page 6 returned HTTP 400 `DIS1035` | **1,282** fetched, **1,282** unique |
| Lost | **82** (6.4%) | **0**: unique IDs = reported total, so the proof passes |
| Calls | 7 | 14 (13 windows + 1 whole-range check) |

An earlier brute-force run that day, at 10:59 PDT, gave the same result: 1,282 reported, 1,200 fetched, 82 lost, page 6 `DIS1035`, 7 calls.

### Done check

| Check | Result |
|---|---|
| `ruff check` | clean |
| `mypy` (strict) | clean, 7 files |
| `pytest` | **131 passed** |
| PLAN's done check | Windows cover the whole range with no gaps (shared edges). Splitting stops at one local day. An over-cap day is reported (1,050 events: all fetched and flagged; 1,300: 1,200 fetched, stopped by `DIS1035`, and the proof fails with exit 1). |
| Daylight saving | Nov 1, 2026 is 25 h with Oct 31 and Nov 2 at 24 h. Mar 14, 2027 is 23 h. The week holding each change is 169 h or 167 h with no gap. Both 01:30s on Nov 1 are fetched. |
| Breakage test | Disabling splitting fails the dense-week test. Counting probe pages as final windows fails it too. |

### Edge probe (18 calls)

- **Both window edges are inclusive.** An event at exactly T is returned by [T−1d, T] and by [T, T+1d], and not by [T−1d, T−1s] or [T+1s, T+1d]. Same for 2 of 2 events.
  - So windows share edges, and no extra second of overlap is needed.
- **A `noSpecificTime` event sits inside its own Los Angeles day**, between 17:00 and 24:00 local. It is not in the UTC day of the same date.

### Surprises

1. **The reported total drifted again:** 1,276 (Phase 0, 15:22 UTC), then 1,280 (Phase 2, 17:17 UTC), then 1,282 (17:59 and 18:01 UTC).
   - The proof therefore compares unique IDs with a total read seconds after the windows, not with an older number.
2. **No window needed splitting in the real run.** The busiest week is about 165 events. Splitting is proven by tests against the fake API, not yet by real data.
3. **Fetched equalled unique (1,282).** No real event fell exactly on a local-midnight edge this time. Edge duplicates are tested, but weren't seen live.

---

## 2026-10-06: Phase 2, HTTP client

### Done check

| Check | Result |
|---|---|
| `ruff check` | clean |
| `mypy` (strict) | clean, 5 files |
| `pytest` | **99 passed** (34 of them client tests) |
| Breakage test | Five client behaviours were broken one at a time. Each made its tests fail, and restoring the code made them pass. The five: httpx's request logging no longer muted; a quota 429 retried; an unusable 200 accepted; httpx's default timeouts used; error responses not counted against the budget. |
| One real call through the client (final query, `size=1`) | HTTP 200. `totalElements` 1,280. `Rate-Limit-Available` went from 4,878 to 4,877. Quota resets at 2026-10-07T15:06:53.158Z. With logging at DEBUG, neither the key nor the text "apikey" appeared anywhere, and the saved params are key-free. |

### PLAN §6 coverage

PLAN §6's test list is all covered:
- 429 then success.
- 401 fails with no retry.
- Five 500s give up.
- Requests are paced at least 250 ms apart.
- The budget guard stops cleanly.
- The key is never in params, logs or errors.

Also covered: quota versus throttle 429s; named timeouts; unusable 200s retried within the same 5-attempt limit; `Retry-After` capped at 60 s.

### Surprises

1. **Ticketmaster's documented quota-exceeded 429 contains the API key** (`"Identifier : {apikey}"`). Error text is now redacted before it reaches any message or log, and there's a test for exactly that body.
2. **Neither 429 code has been observed.**
   - `QuotaViolation` is documented by Ticketmaster.
   - `SpikeArrestViolation` is documented only by Apigee, the gateway Ticketmaster runs on.
   - The fallback (an unlabelled 429 with the budget estimate at 10 or less means quota) covers us if either code differs in practice.
3. **`totalElements` drifted from 1,276 to 1,280 in about 2 hours** (15:22 to 17:17 UTC). The demo number is a snapshot, which is why each run records its own count.

---

## 2026-10-06: Config guards

Two guards, test-first. The full suite has 65 passing tests, and your real `.env` passes both guards.

1. **Tests can never touch production.** A `MARQUEE_TEST_DATABASE_URL` that points at the same database as `MARQUEE_DATABASE_URL` is refused, by both the app and the test fixture. "Same" means the same host (in any letter case), port and database name, so a different username or a reordered query string doesn't get past it.
2. **Locks always get a direct connection.** Any database URL whose host contains `-pooler` is refused. Session-level advisory locks don't hold through Neon's connection pooler.

---

## 2026-10-06: Phase 1 amendment, Marquee's own database variable names

`~/.zshrc` keeps its generic `DATABASE_URL` because other projects need it. Marquee now reads only `MARQUEE_DATABASE_URL` and `MARQUEE_TEST_DATABASE_URL`, and never the generic names. The clash rule still applies to the `MARQUEE_` names. Tests cover three cases:

- A shell `DATABASE_URL` pointing elsewhere (JDBC or a valid Postgres URL) is ignored completely.
- A shell `DATABASE_URL` on its own is not used as a fallback.
- A `MARQUEE_DATABASE_URL` set differently in the shell and in `.env` is refused.

**Done check, rerun with the shell `DATABASE_URL` still exported and no `env -u`:**

| Check | Result |
|---|---|
| `ruff check` | clean |
| `mypy` (strict) | clean, 4 files |
| `pytest` | 56 passed (integration tests against the Neon dev branch) |
| `migrate` run 1 | `applied 0, already applied 1`, exit 0 |
| `migrate` run 2 | `applied 0, already applied 1`, exit 0 |
| Neon main | still 9 tables; `001_init.sql` hash unchanged (`1775fc9d8730`); 0 advisory locks held |

Both runs apply 0 because `001_init.sql` was applied in the first done check. The `env -u DATABASE_URL` workaround in the entry below is no longer needed.

---

## 2026-10-06: Phase 1, skeleton

### Done check

| Check | Result |
|---|---|
| `ruff check` | clean |
| `mypy` (strict, `src/marquee`) | clean, 4 files |
| `pytest`, locally against the Neon dev branch | 51 passed |
| `python -m marquee migrate` on Neon main | run 1: `applied 1 (001_init.sql), already applied 0`; run 2: `applied 0, already applied 1`; both exit 0 |
| Neon main afterwards | 9 tables (8 from PLAN §3 + `schema_migrations`), 0 advisory locks held |
| CI (ruff, mypy, pytest against Postgres 18.6) | green; see the Phase 1 push |

### Surprises

1. **A shell variable nearly chose the database.** `~/.zshrc` (lines 32 and 35) exports `DATABASE_URL` for another project: a JDBC URL to a local `yardsdb`.
   - The first config let the shell win over `.env`, so the first migrate attempt parsed that URL and failed.
   - If it had been a valid Postgres URL, migrate would have created our tables in the other project's database.
   - **Fix:** a variable set differently in the shell and in `.env` is now refused. The error names the variable and never shows the values.
   - Until that `~/.zshrc` line is removed or renamed, run Marquee as `env -u DATABASE_URL uv run python -m marquee …`.
2. **psycopg quotes the connection string in parse errors**, so a malformed URL would print its password in a traceback. **Fix:** `db.connect` removes the password and suppresses the original exception.
3. **Neon runs Postgres 18.6**, where PLAN said 16. Local Docker and CI now use `postgres:18.6`.
4. **One-off command-line overrides are refused locally.** `TEST_DATABASE_URL=… uv run pytest` clashes with `.env` and is refused, which is the intended consequence of fix 1. To test against the local container, put its URL in `.env`. CI has no `.env`, so it's unaffected.

---

## 2026-10-06: Phase 0 close-out, final query and decisions

### The demo number

These come from the final query (`dmaId=324&segmentId=KZFzniwnSyZfZ7v7nJ`, next 90 days: 2026-10-06T15:22:49Z → 2027-01-04T15:22:49Z, `size=200`, `sort=id,asc`).

| | Events |
|---|---|
| **Reported (`totalElements`)** | **1,276** |
| **Reachable with one search** | **1,200** (pages 0–5; page 6 returns HTTP 400 `DIS1035`) |
| **Lost by brute force** | **76 (6.0%)** |
| 13 weekly windows | 1,276 reported, **1,276 received** |

### Run budget, measured

| | Value |
|---|---|
| One run | 13 windows + 2 undated calls = **15 calls**, 1,295 events, **14.46 MB of raw JSON** |
| Hourly calls | 360 a day (budget 2,500) |
| Hourly storage | 347.1 MB of raw JSON a day, so **1.44 days** fit in 500 MB; 3-day retention needs 1,041 MB |
| Every 3 hours | 115.7 MB a day, 4.32 days |

The schedule decision moves to Phase 4, after measuring `pg_total_relation_size`.

### Onsale report (for the onsale-panel decision)

Over the 1,276 windowed events:

- **1,272** have `sales.public.startDateTime`, but **231 of those are the 1900 placeholder**: `1900-01-01T06:00:00Z` or `T18:00:00Z`, with `startTBD` and `startTBA` both false.
- 1,035 real onsale dates are in the past.
- **6 are in the future, all within the next 7 days.**
- The API's `onsaleStartDateTime` filter does *not* select by public start. 0 of 200 events it returned had a start in the requested range, so there's no cheap way to see onsales for shows beyond our 90-day horizon.

### Surprises in this round

1. **`includeTBA=only&includeTBD=only` in one call returns 0.** The flags combine as "both". `includeTBA=only` alone returns 19 (18 postponed); `includeTBD=only` returns 0. So the TBA fix takes **two** undated calls, not one.
2. **`sales.public.startDateTime` has a 1900 placeholder** on 235 of 1,295 events.
3. **The onsale date-range filter behaves like "on sale during the range"**, not like the docs' "onsale start date after this date".

### Decisions recorded

- **LA filter:** `dmaId=324`, labelled "Ticketmaster LA market (DMA 324)". Out-of-area venues stay. A `venues_outside_ca` warning check lists them; today that's 2 venues in Canada, with 3 events.
- **Music filter:** `segmentId=KZFzniwnSyZfZ7v7nJ`.
- **Sort:** `id,asc`.
- **Price snapshots:** cut. Replaced by an append-only `event_changes` table, written by a pure change-detection function, with tests first.
- **TBA gap:** closed with undated calls, two per run (see surprise 1).
- **Storage:** decide in Phase 4 from the measured on-disk size. The fallbacks, in order, are every 3 hours, then store a raw page only when its content hash changed.

---

## 2026-10-06: Phase 0, API probe

### Numbers

These come from the search for the next 90 days of music in DMA 324 (`dmaId=324&classificationName=music`, 2026-10-06T15:06:52Z → 2027-01-04T15:06:52Z).

| | Value |
|---|---|
| **`totalElements`** | **1,317** (7 pages of 200) |
| Brute force (one search, paged to the end) | 1,200 received, **117 lost (8.9%)**; page 6 returned 400 `DIS1035` |
| Weekly windows | 13, largest 168 events, 0 splits needed |
| API calls per run | 13 |
| Raw JSON per run | **14.74 MB** (1,317 events, about 11.4 KB each) |
| API calls used for the probe | 82 (`Rate-Limit-Available` 5,000 → 4,918) |

### Surprises

1. **The paging cap is 1,200, and it's loud.** At `size=200`, page 5 (offset 1,000) still returns 200 events. Page 6 returns 400 `DIS1035`, whose message restates the documented `< 1,000` rule. PLAN §4 says a search "quietly returns 1,000, with no error". Observed: 1,200, then an error.
2. **No `priceRanges` at all.** It's missing on 1,200 of 1,200 events. `price_range_snapshots` would stay empty.
3. **Storage, not calls, is the binding budget.** Hourly runs use 353.7 MB of raw JSON a day. With 3-day retention that's 1,061 MB against the 500 MB budget, 2.1× over. On-disk size after Postgres compression is unmeasured until Phase 4.
4. **`classificationName=music` lets in 41 non-Music events:** 1,317 versus 1,276 with the Music `segmentId`. Of the 1,200 fetched, 20 are Arts & Theatre, 11 Undefined and 5 Film.
5. **DMA 324 is wide.** It covers 47 cities in 600 events, out to Palm Springs, Coachella, Paso Robles and San Luis Obispo. Two venues in Canada (3 events) are tagged 324.
6. **The `includeTBA` default contradicts the docs.** It excludes TBA events even when no date filter is sent: 1,643 by default versus 1,663 with `includeTBA=yes`. **20 TBA events (19 postponed) are invisible to dated windows.**
7. **Status is spelled `cancelled`** (27 events). PLAN §3 says `canceled`.
8. **Venue `latitude` and `longitude` are strings.** `dates.timezone` is missing on 258 of 1,200 events; `venue.timezone` is never missing.
9. **400 responses count against the quota but carry no rate-limit headers.**
10. **The `size` error message says "must be less than 200",** but 200 is accepted.

### Decisions needed

- **LA filter:** `dmaId=324` (1,317; recommended) or `city=Los Angeles&stateCode=CA` (777, which is under the cap). Optionally add `countryCode=US`.
- **Music filter:** `classificationName=music` (1,317) or `segmentId=KZFzniwnSyZfZ7v7nJ` (1,276; recommended).
- **Sort:** `id,asc` (recommended: a strict order on a unique key) or `date,asc` as originally specified. There's no date-plus-ID option.
- **Schedule and retention:** decide after Phase 4 measures real on-disk bytes per run. Hourly with 3-day retention needs ≤ 6.9 MB per run on disk. Every 3 hours fits now (354 MB).
- **Price snapshots:** the data isn't there. Cut them now (they're first on the cut line), or keep the table and expect 0 rows.
- **TBA gap:** accept it as a known gap, or add 1 undated `includeTBA=only` call per run. Either way, `last_seen_run` must not treat an event that becomes TBA as vanished.
- **PLAN wording:** §4 (the cap behaviour) and §3 (`cancelled`) don't match what we observed.
