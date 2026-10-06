# Ticketmaster Discovery API: observed behaviour

Probed on **2026-10-06, 15:06–15:11 UTC** with `scratch/probe.py` and `scratch/probe_followup.py`, using 83 calls in total. The raw outputs are in `local/probe_results.json` and `local/probe_followup_results.json` (both gitignored).

Every value here was **observed** in those runs unless it is marked **(docs)**. Counts are a snapshot and will drift from hour to hour.

## At a glance

| | Observed |
|---|---|
| Endpoint | `GET https://app.ticketmaster.com/discovery/v2/events.json` |
| Auth | `apikey` query parameter |
| Max page size | **200** (201 and 500 are rejected) |
| Deepest page served | offset 1,000 (page 5 at size 200), so at most **1,200 events per search** |
| Past that | **HTTP 400 `DIS1035`**: loud, not silent |
| LA music, next 90 days | **`totalElements` = 1,317**, `totalPages` = 7 (`dmaId=324&classificationName=music`) |
| Brute-force single search | 1,200 received, **117 lost (8.9%)** |
| Full page (200 events) | 1.6–2.9 MB of JSON, 0.11–0.32 MB gzipped on the wire, about 11.4 KB per event |
| Daily quota | `Rate-Limit: 5000` |
| Date format accepted | `YYYY-MM-DDTHH:mm:ssZ` only |
| `priceRanges` | **absent on all 1,200 events fetched** |

---

## 1. Endpoint and auth

- **Base and path:** `https://app.ticketmaster.com` + `/discovery/v2/events.json`.
- **Key:** sent as the `apikey` query parameter (docs). Every successful call used it. An invalid key returns 401 (§9).
- **Key exposure:** the key travels in the URL, so any logged URL leaks it. Never log request URLs. The API itself never echoes the key back (§10).

## 2. The search we run

```
dmaId=324  classificationName=music          # or segmentId, see §4
startDateTime=YYYY-MM-DDTHH:mm:ssZ  endDateTime=YYYY-MM-DDTHH:mm:ssZ
size=200  sort=id,asc  page=N
```

## 3. The LA filter

| Filter | `totalElements` (90 days, music) |
|---|---|
| `dmaId=324` | **1,317** |
| `city=Los Angeles&stateCode=CA` | 777 |

**What DMA 324 covers.** A DMA (designated market area) is a TV-market region code. I tallied venue cities across 600 events (`date,asc` pages 0, 1 and 4) and found **47 distinct cities**:

- **Core LA:** Los Angeles 285, Santa Ana 35, Anaheim 35, Hollywood 34, Inglewood 31, West Hollywood 22.
- **Inland Empire and desert:** Riverside 18, Highland 15, Pioneertown 14, Palm Springs 9, Coachella 5.
- **Central Coast:** Ventura 7, Paso Robles 5, San Luis Obispo 2, Santa Barbara 1.
- **Data errors:** across all 1,200 fetched events (124 venues), **2 venues in Canada** (Ontario and British Columbia, 3 events) are tagged DMA 324. One venue city has a leading space: `" Long Beach"`.
- **Venue `dmas[].id`:** every venue carried 324. 451 of the 600 events' venues also carried 223, 354 and 383.

**The city filter drops core venues.** `city=Los Angeles` excludes Inglewood, Anaheim, Hollywood and West Hollywood, which are 122 of the 600 events tallied. It also brings the 90-day total under 1,000, so the cap problem disappears.

**Recommendation: `dmaId=324`, labelled "Greater LA market".** Two optional refinements: add `countryCode=US` to drop the Canadian strays, and add a check for venues outside CA. *Pending your decision.*

## 4. The music filter

| Filter (with `dmaId=324`, 90 days) | `totalElements` |
|---|---|
| `classificationName=music` | 1,317 |
| `segmentId=KZFzniwnSyZfZ7v7nJ` (the Music segment) | 1,276 |
| `segmentName=Music` | 1,276 |

`classificationName` matches a name at any level of the classification (docs: "name of any segment, genre, sub-genre, type, sub-type"). Of the 1,200 events it returned, the primary segment was:

| Primary segment | Events |
|---|---|
| Music | 1,164 |
| Arts & Theatre | 20 (e.g. genre Classical › Symphonic) |
| Undefined | 11 |
| Film | 5 (genre "Music", i.e. concert films) |

**Recommendation: `segmentId=KZFzniwnSyZfZ7v7nJ`.** It's an exact ID, not fuzzy name matching. *Pending your decision.*

## 5. Paging, and where it stops

- **Response metadata:** `page.number` (zero-based), `page.size`, `page.totalElements`, `page.totalPages`. `totalPages` equals ceil(total / size): 1,317 / 200 gives 7.
- **Links:** `_links.first`, `.self`, `.next` and `.last` hold relative URLs that echo our parameters without `apikey` (§10).
- **Size:** `size=200` works. `size=201` and `size=500` both return:
  ```json
  {"errors":[{"_links":{"about":{"href":"/discovery/v2/errors.html#DIS1036"}},"code":"DIS1036","detail":"Query param \"size\" must be less than 200","status":"400 BAD_REQUEST"}]}
  ```
  The message is off by one, since 200 itself is accepted.

**Where deep paging stops.** This is a full sweep of the 90-day search with `size=200`. Both `date,asc` and `id,asc` behaved identically.

| page | `page × size` | status | events |
|---|---|---|---|
| 0–4 | 0–800 | 200 | 200 each |
| 5 | **1,000** | **200** | **200** (events 1,001–1,200) |
| 6 | 1,200 | **400** | none |

Page 6 returns exactly:

```json
{"errors":[{"_links":{"about":{"href":"/discovery/v2/errors.html#DIS1035"}},"code":"DIS1035","detail":"API Limits Exceeded: Max paging depth exceeded. (page * size) must be less than 1,000","status":"400 BAD_REQUEST"}]}
```

- **The docs rule isn't what's enforced.** The docs say `size * page < 1000`, but page 5 (`size × page` = 1,000) was served. The same held at `size=20`, where page 50 (offset 1,000) returned 20 events. I didn't test the exact boundary between offsets 1,000 and 1,200.
- **The real brute-force result:** 1,317 reported, 1,200 received, **117 lost (8.9%)**. The loss is loud (a 400 on page 6), not a quiet stop at 1,000.
  - A client that stops at the documented limit (page 4) would silently get only 1,000 and lose 317.
  - Which events are lost depends on the sort. `date,asc` loses the latest dates; `id,asc` loses events scattered across the whole range. The two sweeps' sets differed by 107 events each way.
- **A second cap, for completeness:** a US-wide music search reported `totalElements` of exactly **10,000** and `totalPages` of 500, so totals are capped too. Pages at offset 9,980 and beyond returned:
  ```json
  {"errors":[{"_links":{"about":{"href":"/discovery/v2/errors.html#DIS1024"}},"code":"DIS1024","detail":"Results limit exceeded. Please refine your search criteria.","status":"400"}]}
  ```
  LA never comes close to this.
- **Implication for windows:** keep the split threshold at the documented **1,000** even though 1,200 worked. It's a safety margin if Ticketmaster fixes the off-by-one.

## 6. Sort order and paging stability

**Options.** The default is `relevance,desc` (docs). The documented options are:

`name,asc|desc` · `date,asc|desc` · `relevance,asc|desc` · `distance,asc` · `name,date,asc|desc` · `date,name,asc|desc` · `distance,date,asc` · `onSaleStartDate,asc` · `id,asc` · `venueName,asc|desc` · `random`

**No option combines date with an ID tie-breaker.** `id,asc` exists on its own.

| Sort | IDs on both page 0 and page 1 | Full sweep received / unique |
|---|---|---|
| `date,asc` | 0 | 1,200 / 1,200 |
| `id,asc` | 0 | 1,200 / 1,200 |
| `relevance,desc` (default) | 0 | not swept |

- **Ties under `date,asc` are common.** On page 0, **171 of 200 events** share their `dates.start.dateTime` with at least one other event (34 groups). The last event on page 0 and the first on page 1 share the same timestamp, and **9 events** across the two pages carry it. The docs don't define the order within a tie. Fetching page 0 twice gave the same order, but that's one sample and proves nothing about later.
- **`id,asc` is a strict order.** IDs came back strictly ascending in byte order (digits < uppercase < lowercase), within each page and across pages.

**Chosen: `sort=id,asc`.** You asked for `date,asc`; this departs from that, and it's a one-line change to switch back.

- The ID is unique, so every event has exactly one position and ties can't shuffle across page boundaries.
- We don't need date order, because each window already sets the date bounds.
- No sort protects against inserts or deletes in the middle of a sweep. If an event is removed before the cursor, the next one slides back onto a page we've already fetched. The `fetched_vs_reported` check catches that, and the next run picks the event up.

## 7. Dates

- **`startDateTime` / `endDateTime` accept only `YYYY-MM-DDTHH:mm:ssZ`.**

| Value | Result |
|---|---|
| `2026-10-06T15:06:52Z` | 200 |
| `2026-10-06T15:06:52.000Z` (milliseconds) | 400 |
| `2026-10-06T15:06:52` (no `Z`) | 400 |

  Both 400s return:
  ```json
  {"errors":[{"_links":{"self":{"href":"/discovery/v2/errors.html#DIS1015"}},"code":"DIS1015","detail":"Query param with date must be of valid format YYYY-MM-DDTHH:mm:ssZ {example: 2020-08-01T14:00:00Z }","status":"400 BAD_REQUEST"}]}
  ```
- **Window edges:** 13 weekly windows with shared edges summed to 1,317, the same as the single 90-day search. No double counting showed at the boundaries (they fell at :52 seconds). I didn't test whether the bounds are inclusive or exclusive.
- **Date fields in responses:**

| Field | Format |
|---|---|
| `dates.start.localDate` | `YYYY-MM-DD` |
| `dates.start.localTime` | `HH:mm:ss` |
| `dates.start.dateTime` | UTC, `YYYY-MM-DDTHH:mm:ssZ` |
| `dates.start.dateTBD`, `dateTBA`, `timeTBA`, `noSpecificTime` | booleans, present on all 200 sample events |
| `sales.public.startDateTime`, `endDateTime` | UTC, `YYYY-MM-DDTHH:mm:ssZ` |
| `sales.public.startTBD`, `startTBA` | booleans |

## 8. TBA and TBD events (known gap)

The docs say `includeTBA` and `includeTBD` default to "no if date parameter sent, yes otherwise".

| Search (DMA 324, music) | `totalElements` |
|---|---|
| dated (90 days), default | 1,317 |
| dated, `includeTBA=yes&includeTBD=yes` | 1,318 |
| dated, `includeTBA=only` | 1 |
| dated, `includeTBD=only` | 0 |
| undated, default | 1,643 |
| undated, `includeTBA=no` | 1,643 |
| undated, `includeTBA=yes` | 1,663 |
| undated, `includeTBA=only` | **20** |
| undated, `includeTBD=only` | 0 |

- **The default is effectively "no" even without date parameters.** Undated-default equals `includeTBA=no`, which contradicts the docs.
- **The 20 TBA events:** all have `dateTBA=true` and **no `localDate` or `dateTime`**. 19 are `postponed` and 1 is `onsale`. 9 carry `dates.initialStartDate`, their original date.
- **One odd case:** a dated search with `includeTBA=only` matched 1 event that has no date fields at all (`dateTBA` and `timeTBA` both true). I can't explain why it matched.
- **The `id` filter works:** a normal event's ID returns 1. A TBA event's ID returns 0 on its own, and 1 with `includeTBA=yes` or `only`.
- **Inside dated results:** no event had `dateTBA`, `dateTBD` or `timeTBA` set. 4 of 1,200 had `noSpecificTime=true`; those have a `localDate` but no `localTime` or `dateTime`.

**Known gap: our windowed searches will never see about 20 TBA events, mostly postponed shows.**

- **Cost to close it:** one undated `includeTBA=only` call per run (about 197 KB).
- **Side effect:** when a show is postponed to TBA, it drops out of every window. `last_seen_run` must not read that as "vanished".

## 9. Errors

There are two different shapes:

1. **Discovery errors (400):** `{"errors":[{"_links":{…},"code":"DIS####","detail":"…","status":"…"}]}`.
   - The `_links` key varies: `about` for DIS1036, DIS1035 and DIS1024; `self` for DIS1015.
   - So does `status`: `"400 BAD_REQUEST"`, except DIS1024, which uses `"400"`.

| Code | Meaning |
|---|---|
| DIS1036 | `size` too large |
| DIS1015 | bad date format |
| DIS1035 | paging too deep |
| DIS1024 | results limit |

2. **Gateway auth error (401), invalid key:** `Content-Type: application/json`, `Content-Length: 90`, body
   ```json
   {"fault":{"faultstring":"Invalid ApiKey","detail":{"errorcode":"oauth.v2.InvalidApiKey"}}}
   ```
   It **does not echo the key** it was given.

- **No rate-limit headers** on any 400 or 401 response.
- **Not observed:** 403, 404, 429, 5xx, `Retry-After`.

## 10. Is the API key in responses?

| Scan | Result |
|---|---|
| 39 responses in run 1 (including 400 and 401 bodies), body and every header | 0 contain the key |
| All 12,147 `_links` `href`s | 0 contain the key; 0 contain the text "apikey" |
| Follow-up DIS1035 and DIS1024 bodies, printed in full | no key |
| Every file saved in `local/` | key absent |

`href`s are relative and echo our other parameters. For example:
`/discovery/v2/events.json?startDateTime=2026-10-06T15%3A06%3A52Z&dmaId=324&classificationName=music&endDateTime=2027-01-04T15%3A06%3A52Z&page=1&size=200&sort=date,asc`

**Result: Phase 4 can store response bodies as received.** The `params` we save are built by us, so we still strip `apikey` from those.

## 11. Rate limits

- **Headers on every 200 response:**

| Header | Value |
|---|---|
| `Rate-Limit` | `5000` |
| `Rate-Limit-Available` | e.g. `4997` |
| `Rate-Limit-Over` | `0` |
| `Rate-Limit-Reset` | `1791385613158` (epoch milliseconds) |

- **Reset time:** `Rate-Limit-Reset` = **2026-10-07T15:06:53.158Z**, 24 hours after our first call (15:06:52Z). It didn't change across all 82 counted calls.
- **400s count against the quota.**
  - Run 1 opened with two 400s, and the first 200 showed 4,997 available.
  - After the call that showed 4,964 came two 400s and one 401 sent with a fake key (which can't count against our key). The next 200 showed 4,961: three fewer, meaning the two 400s plus itself.
  - Because 400s carry no rate-limit headers, **the budget guard must count errors itself.** It can only read `Available` from successful responses.
- **Used today:** 82 calls (5,000 → 4,918).
- **Per-second limit:** 5 per second (docs). Not tested: we paced every call at least 300 ms apart and saw no 429s in 83 calls. `Rate-Limit-Over` stayed at `0`.
- **Latency:** a full 200-event page took 0.57–0.95 s; a `size=1` call took 0.10–0.32 s.

## 12. IDs

| Entity | Shape (observed) |
|---|---|
| Event `id` | 13–21 chars, `[A-Za-z0-9_-]`, case-sensitive, mixed prefixes (`vv` 152, `Z7` 40, `Lv` 5, `ZF` 2, `1A` 1 of 200) |
| Venue `id` | 10–12 chars (prefixes `Kov`, `ZFr`, `Z6r`) |
| Attraction `id` | 10–12 chars (prefixes `K8v`, `Z7r`) |
| Music segment | `KZFzniwnSyZfZ7v7nJ` |

- **Uniqueness:** event IDs were unique within every page and across full sweeps (0 duplicates in 1,200). Store them as `text` and compare case-sensitively.
- **The event `url` often doesn't contain the API `id`:** 149 of 200 end in a 16-character hex ID, 40 of 200 end in the API ID, and the rest have other shapes. **Never parse IDs out of URLs.**
- **Venues and attractions per event:** every event had **exactly 1 venue** (1,200 of 1,200). Attractions ranged from 0 to 7.

## 13. Fields that are sometimes missing

"Sample" is the saved `local/sample_events.json`: page 0, 200 events. "Sweep" is all 1,200 events from the `date,asc` sweep. "—" means not counted.

| Field (for the data model) | Missing in sample | Missing in sweep | Notes |
|---|---|---|---|
| `priceRanges` | **200 / 200** | **1,200 / 1,200** | **No face-value prices at all** |
| `dates.timezone` | 40 / 200 | 258 / 1,200 | `venue.timezone` present 1,200 / 1,200, so use it as the fallback |
| `dates.start.localTime`, `dateTime` | 0 / 200 | 4 / 1,200 | `noSpecificTime=true` |
| `_embedded.venues[0].location` | 2 / 200 | 13 / 1,200 | **`latitude` and `longitude` are JSON strings, not numbers** (all 198 present) |
| `_embedded.attractions` | 1 / 200 | 15 / 1,200 | |
| `_embedded.venues` | 0 / 200 | 0 / 1,200 | always exactly one |
| `name`, `url`, `dates.start.localDate`, `dates.status.code`, `sales.public.startDateTime`, `classifications[0].segment` and `.genre`, `venue.name`, `city.name`, `state.stateCode`, `timezone` | 0 / 200 | — | |

**Values to handle:**

- **`dates.status.code`** across the sweep: `onsale` 1,144, **`cancelled`** 27, `rescheduled` 20, `offsale` 9. `postponed` also appears in the TBA sample. The API spells it `cancelled`; PLAN §3 says `canceled`.
- **Genre placeholders** are strings, not nulls: `"Other"` 125, `"Undefined"` 30 (sweep).
- **Venue city names can have stray whitespace** (`" Long Beach"`), so strip them.
- **No test events:** `test=true` on 0 events.
- **Optional blocks on some events only** (sample counts out of 200):

| Block | Events |
|---|---|
| `promoter` | 149 |
| `sales.presales` | 107 |
| `doorsTimes` | 34 |
| `dates.end` | 7 |
| `dates.initialStartDate` | 2 |
| `description` | 6 |
| `classifications[].subGenre` | 198 |

## 14. Response size

| | Range |
|---|---|
| Full page (200 events), JSON | **1,602–2,912 KB**, about **11.4 KB per event** |
| Same pages gzipped on the wire | 112–325 KB |

- 200 responses are gzipped and **chunked, with no `Content-Length`**.
- Most of the weight is optional detail: `images`, embedded venue and attraction objects, `linkMoreInfo` in 10 locales, `ticketTextLines`. No query parameter we found trims the fields returned.

## 15. Budgets for an hourly schedule (PLAN §4)

These are measured on a real "run": the 13 weekly windows at `size=200`. Every window held 168 events or fewer, so each needed one page and none needed splitting.

| | Per run |
|---|---|
| API calls | **13** |
| Events | 1,317 |
| Raw JSON | **14.74 MB** |
| gzip of the same bytes (shows how compressible it is) | 1.72 MB |

**API calls: fits.**

- Hourly is 13 × 24 = **312 calls a day**, against a budget of 2,500. That's 8× headroom.
- Adding the TBA call makes it 336 a day.

**Storage: does not fit hourly with the default 3-day retention, measured on JSON text.**

| Schedule | Raw MB per day | Days that fit in 500 MB | 3-day retention needs |
|---|---|---|---|
| every 1 h | 353.7 | **1.41** | 1,061 MB, **2.1× over** |
| every 2 h | 176.9 | 2.83 | 531 MB, just over |
| every 3 h | 117.9 | 4.24 | 354 MB, fits |

**Caveat.** That's the size of the JSON text. Postgres stores `jsonb` and compresses large values (TOAST). The same bytes gzip 8.6× smaller, but Postgres's compression is weaker than gzip, so the real number is unknown until it's measured.

Hourly with 3-day retention fits if the real on-disk size is **6.9 MB per run or less**, which is at least 2.1× smaller than the JSON text. **Measure it in Phase 4** with `pg_total_relation_size('raw_responses')` before choosing the schedule, and record the result in decision record 003.
