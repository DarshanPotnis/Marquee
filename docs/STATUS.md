# Status

Newest first. Every number here comes from a real run, never an estimate. Full detail is in `docs/api-notes.md`.

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
