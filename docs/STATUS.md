# Status

Newest first. Every number here comes from a real run, never an estimate. Full detail is in `docs/api-notes.md`.

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
