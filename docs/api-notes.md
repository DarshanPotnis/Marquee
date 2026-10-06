# Ticketmaster Discovery API: observed behaviour

Probed on **2026-10-06, 15:06–15:24 UTC** with four scripts in `scratch/`:

| Script | Purpose |
|---|---|
| `probe.py` | first probe |
| `probe_followup.py` | follow-up on surprises |
| `probe_final.py` | final query |
| `probe_size.py` | per-run size, placeholders |

That's 123 calls in total. The raw outputs are in `local/probe_*results.json` (gitignored).

Every value here was **observed** unless it's marked **(docs)**. "Docs" means the Discovery API v2 page (`developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/`), read on 2026-10-06. Counts are a snapshot and will drift.

Two queries appear below:

| Query | Parameters | Used where |
|---|---|---|
| **Final** | `dmaId=324&segmentId=KZFzniwnSyZfZ7v7nJ` | the pipeline and the demo |
| First probe | `dmaId=324&classificationName=music` | kept only where it explains a decision |

## At a glance

| | Observed |
|---|---|
| Endpoint | `GET https://app.ticketmaster.com/discovery/v2/events.json` |
| Auth | `apikey` query parameter |
| Max page size | **200** |
| Reachable per search | **1,200** at size 200 (pages 0–5). Page 6 returns **HTTP 400 `DIS1035`**. The docs say 1,000. |
| **Final query, next 90 days** | **1,276 reported, 1,200 reachable, 76 lost (6.0%)** |
| Same query, 13 weekly windows | 1,276 reported, 1,276 received |
| Undated TBA events (not in any window) | 19 |
| One run (13 windows + 2 undated calls) | **15 calls, 14.46 MB of raw JSON** |
| Daily quota | `Rate-Limit: 5000` |
| Date format accepted | `YYYY-MM-DDTHH:mm:ssZ` only |
| `priceRanges` | absent on all 1,200 events checked |
| Onsale date placeholder | `sales.public.startDateTime` = `1900-01-01T…` on **235 of 1,295** events (231 of the 1,276 windowed); stored as NULL |

---

## 1. Docs versus reality

Every difference we found between what the docs say and what the API did.

| # | Topic | Docs say | Observed |
|---|---|---|---|
| 1 | Deep paging | "we only support retrieving the 1000th item. i.e. ( size * page < 1000)" | Page 5 at size 200 (`size × page` = 1,000) is served, so **1,200** events are reachable. Page 6 returns 400 `DIS1035`, whose own message repeats "must be less than 1,000". Page 50 at size 20 (offset 1,000) is also served. |
| 2 | `includeTBA` / `includeTBD` default | "no if date parameter sent, yes otherwise" | **Without** a date filter the default still excludes TBA events. Undated default: 1,643. `includeTBA=no`: 1,643. `includeTBA=yes`: 1,663. |
| 3 | Combining `includeTBA=only` and `includeTBD=only` | Not stated | Together they return **0**. Alone they return 19 and 0. Together they mean "both", so one combined call can't fetch every undated event. |
| 4 | `onsaleStartDateTime` / `onsaleEndDateTime` | "Filter with onsale start date after this date" / "…onsale end date before this date" | With start = now and end = now + 7 days, 1,583 events matched. On page 0, **none** of the 200 had a public start inside that range: 69 were in the past and 131 were the 1900 placeholder. 174 of 200 had a public end *after* the range. It behaves like "on sale during the range". |
| 5 | Max `size` | Not stated (only "Page size of the response", default 20) | 200 works. 201 and 500 return 400 `DIS1036`, whose message says "must be less than 200", although 200 itself works. |
| 6 | Date format for `startDateTime` / `endDateTime` | Not stated ("Filter with a start date after this date") | Only `YYYY-MM-DDTHH:mm:ssZ`. Milliseconds or a missing `Z` return 400 `DIS1015`, and the error message states the format. |
| 7 | Total results cap | Not stated | Large searches report `totalElements` = **10,000** exactly. Pages from offset 9,980 on return 400 `DIS1024` "Results limit exceeded". |
| 8 | Error format | Shows only the 401 `{"fault":…}` shape | The 401 matches. 400s use a different `{"errors":[…]}` shape, which isn't shown, and its fields vary between codes (§10). |
| 9 | Rate limits | "5000 API calls per day and rate limitation of 5 requests per second"; header names listed | `Rate-Limit: 5000` matches. Not stated in the docs: the reset is 24 h after the first call; **400s count** against the quota; 400 and 401 responses carry **no** rate-limit headers. |
| 10 | `sales.public.startDateTime` | No placeholder mentioned | 235 of 1,295 events carry `1900-01-01T06:00:00Z` or `1900-01-01T18:00:00Z`, with `startTBD` and `startTBA` both false (§9). |

**Matches the docs, but surprising:** `classificationName` does match "any segment, genre, sub-genre, type, sub-type". That's why `classificationName=music` also returns Film and Arts & Theatre events (§4).

## 2. Endpoint, auth and the final query

- **Base and path:** `https://app.ticketmaster.com` + `/discovery/v2/events.json`.
- **Key:** sent as the `apikey` query parameter (docs). An invalid key returns 401 (§10). The key travels in the URL, so never log request URLs. The API never echoes the key back (§11).

**Final query.** Each run makes 13 weekly windowed calls plus 2 undated calls:

```
windows:  dmaId=324  segmentId=KZFzniwnSyZfZ7v7nJ
          startDateTime=YYYY-MM-DDTHH:mm:ssZ  endDateTime=YYYY-MM-DDTHH:mm:ssZ
          size=200  sort=id,asc  page=N
undated:  dmaId=324  segmentId=KZFzniwnSyZfZ7v7nJ  includeTBA=only  size=200  sort=id,asc
          dmaId=324  segmentId=KZFzniwnSyZfZ7v7nJ  includeTBD=only  size=200  sort=id,asc
```

## 3. The LA filter: "Ticketmaster LA market (DMA 324)"

**Decision:** use `dmaId=324` and label it **"Ticketmaster LA market (DMA 324)"**. Keep the out-of-area venues, and flag them with the `venues_outside_ca` warning check.

| Filter (first probe, 90 days) | `totalElements` |
|---|---|
| `dmaId=324` | 1,317 |
| `city=Los Angeles&stateCode=CA` | 777 |

**What DMA 324 contains.** A DMA (designated market area) is a TV-market region code. Venue cities across 600 first-probe events came to **47 distinct cities**:

- **Core LA:** Los Angeles 285, Santa Ana 35, Anaheim 35, Hollywood 34, Inglewood 31, West Hollywood 22.
- **Inland Empire and desert:** Riverside 18, Highland 15, Pioneertown 14, Palm Springs 9, Coachella 5.
- **Central Coast:** Ventura 7, Paso Robles 5, San Luis Obispo 2, Santa Barbara 1.
- **Venue `dmas[].id`:** every venue carried 324; 451 of the 600 events' venues also carried 223, 354 and 383.

The city filter would drop Inglewood, Anaheim, Hollywood and West Hollywood: 122 of those 600 events.

**`venues_outside_ca` preview (final query, 1,276 events):** **2 venues, 3 events**, both in Canada (one in Ontario, one in British Columbia), tagged DMA 324. One CA venue's city name has a leading space (`" Long Beach"`).

## 4. The music filter: Music segment ID

**Decision:** `segmentId=KZFzniwnSyZfZ7v7nJ`.

| Filter (with `dmaId=324`, 90 days) | `totalElements` |
|---|---|
| `classificationName=music` | 1,317 |
| `segmentId=KZFzniwnSyZfZ7v7nJ` | **1,276** |
| `segmentName=Music` | 1,276 |

Of 1,200 events returned by `classificationName=music`, the primary segment was:

| Primary segment | Events |
|---|---|
| Music | 1,164 |
| Arts & Theatre | 20 (e.g. Classical › Symphonic) |
| Undefined | 11 |
| Film | 5 (genre "Music") |

## 5. Paging, and where it stops

- **Response metadata:** `page.number` (zero-based), `page.size`, `page.totalElements`, `page.totalPages`. `totalPages` equals ceil(total / size).
- **Links:** `_links.first`, `.self`, `.next` and `.last` hold relative URLs that echo our parameters without `apikey`.
- **Empty result:** `{"_links": …, "page": {"size": 200, "totalElements": 0, "totalPages": 0, "number": 0}}`. There's no `_embedded` and `totalPages` is **0**, so a paging loop must not assume at least one page.
- **Size:** 200 works. 201 and 500 return:
  ```json
  {"errors":[{"_links":{"about":{"href":"/discovery/v2/errors.html#DIS1036"}},"code":"DIS1036","detail":"Query param \"size\" must be less than 200","status":"400 BAD_REQUEST"}]}
  ```

**Brute force with the final query.** One 90-day search (2026-10-06T15:22:49Z → 2027-01-04T15:22:49Z), `size=200`, `sort=id,asc`:

| page | `page × size` | status | events |
|---|---|---|---|
| 0–4 | 0–800 | 200 | 200 each |
| 5 | **1,000** | **200** | 200 |
| 6 | 1,200 | **400** | none |

**Result: 1,276 reported, 1,200 received (1,200 unique), 76 lost (6.0%).** The first probe's query behaved the same way: 1,317 reported, 1,200 received, 117 lost.

Page 6 returns exactly:

```json
{"errors":[{"_links":{"about":{"href":"/discovery/v2/errors.html#DIS1035"}},"code":"DIS1035","detail":"API Limits Exceeded: Max paging depth exceeded. (page * size) must be less than 1,000","status":"400 BAD_REQUEST"}]}
```

- **The loss is loud, not silent.** But a client that stops at the documented limit (page 4) would silently get 1,000.
- **Which events are lost depends on the sort.** `date,asc` loses the latest dates; `id,asc` loses events scattered across the range. In the first probe, the two sweeps' sets differed by 107 events each way.
- **Windowed with the final query:** 13 weekly windows reported 156, 165, 142, 124, 130, 130, 121, 72, 92, 72, 55, 7 and 10 events, **summing to 1,276. All 1,276 were received**, one page per window, with no splits.
- **Edges and bounds:** shared window edges caused no double counting. I didn't test whether the bounds are inclusive or exclusive.
- **Split threshold: stays at the documented 1,000**, even though 1,200 is reachable. It's a margin in case the off-by-one gets fixed.
- **The total cap** (docs-vs-reality #7) never comes close at LA scale.

## 6. Sort order: `id,asc`

**Decision:** `sort=id,asc` (agreed).

**Options.** The default is `relevance,desc` (docs). The documented options are:

`name,asc|desc` · `date,asc|desc` · `relevance,asc|desc` · `distance,asc` · `name,date,asc|desc` · `date,name,asc|desc` · `distance,date,asc` · `onSaleStartDate,asc` · `id,asc` · `venueName,asc|desc` · `random`

There's no date-plus-ID option.

| Sort (first probe) | IDs on both page 0 and page 1 | Full sweep received / unique |
|---|---|---|
| `date,asc` | 0 | 1,200 / 1,200 |
| `id,asc` | 0 | 1,200 / 1,200 |
| `relevance,desc` | 0 | not swept |

- **Ties under `date,asc`:** 171 of 200 events on page 0 share their `dates.start.dateTime` with at least one other event (34 groups). The last event on page 0 and the first on page 1 share a timestamp, and 9 events across the two pages carry it.
- **`id,asc`:** IDs came back strictly ascending in byte order (digits < uppercase < lowercase), within and across pages.

**Why `id,asc`:**

- The ID is unique, so the order is strict and ties can't shuffle across page boundaries.
- Each window already provides the date bounds, so we don't need date order.
- No sort protects against inserts or deletes mid-sweep. `fetched_vs_reported` catches those, and the next run repairs them.

## 7. Dates

- **`startDateTime` / `endDateTime` accept only `YYYY-MM-DDTHH:mm:ssZ`.**

| Value | Result |
|---|---|
| `2026-10-06T15:06:52Z` | 200 |
| `2026-10-06T15:06:52.000Z` | 400 |
| `2026-10-06T15:06:52` | 400 |

  Both 400s return:
  ```json
  {"errors":[{"_links":{"self":{"href":"/discovery/v2/errors.html#DIS1015"}},"code":"DIS1015","detail":"Query param with date must be of valid format YYYY-MM-DDTHH:mm:ssZ {example: 2020-08-01T14:00:00Z }","status":"400 BAD_REQUEST"}]}
  ```
- **Date fields in responses:**

| Field | Format |
|---|---|
| `dates.start.localDate` | `YYYY-MM-DD` |
| `dates.start.localTime` | `HH:mm:ss` |
| `dates.start.dateTime` | UTC, `YYYY-MM-DDTHH:mm:ssZ` |
| `dates.start.dateTBD`, `dateTBA`, `timeTBA`, `noSpecificTime` | booleans, present on all 200 sample events |
| `sales.public.startDateTime`, `endDateTime` | UTC |
| `sales.public.startTBD`, `startTBA` | booleans |

## 8. TBA and TBD events: two undated calls per run

The docs default is covered in docs-vs-reality #2. First probe:

| Search (DMA 324, `classificationName=music`) | `totalElements` |
|---|---|
| dated (90 days), default | 1,317 |
| dated, `includeTBA=yes&includeTBD=yes` | 1,318 |
| dated, `includeTBA=only` | 1 |
| undated, default | 1,643 |
| undated, `includeTBA=no` | 1,643 |
| undated, `includeTBA=yes` | 1,663 |

Final query, undated (`dmaId=324&segmentId=…`, no date filter):

| Flags | `totalElements` | Detail |
|---|---|---|
| `includeTBA=only&includeTBD=only` | **0** | |
| `includeTBA=only` | **19** | all `dateTBA=true`, none has `localDate`; 18 `postponed`, 1 `onsale`; 2 also `timeTBA` |
| `includeTBD=only` | 0 | |

- **No overlap with the windows:** none of the 19 appear in the 1,276 windowed events.
- **One odd case:** in the first probe, a dated search with `includeTBA=only` matched 1 event that has no date fields at all. I can't explain it.
- **The `id` filter needs the flag too:** it returns a normal event, but a TBA event's ID returns 0 unless `includeTBA=yes` or `only` is added.
- **Within dated results:** none had `dateTBA`, `dateTBD` or `timeTBA` set. 4 of 1,200 had `noSpecificTime=true`; those have a `localDate` but no `localTime` or `dateTime`. The postponed TBA events often carry `dates.initialStartDate`, their original date (9 of 20 in the first probe).

**Decision: close the gap.** The combined call returns nothing (docs-vs-reality #3), so it takes **two undated calls per run, `includeTBA=only` and `includeTBD=only`**. Together they cost 188.7 KB per run. Because a postponed show stays visible through the undated call, `last_seen_run` won't mistake it for a vanished one.

## 9. Public onsale data (`sales.public`)

Measured over the 1,276 windowed events of the final query, at 2026-10-06T15:22:49Z:

| | Events |
|---|---|
| With `sales.public.startDateTime` | 1,272 |
| Without it (all 4 have `startTBA=true`) | 4 |
| … of the 1,272: the **1900 placeholder** | 231 |
| … real, in the past | 1,035 |
| … **real, in the future** | **6** (all within the next 7 days; 2 also list a presale) |

- **The placeholder values**, counted over 1,295 events (windows plus undated):

| Value | Events |
|---|---|
| `1900-01-01T06:00:00Z` | 184 |
| `1900-01-01T18:00:00Z` | 51 |

  `startTBD` and `startTBA` are false on all 235, and their statuses are `onsale` 230, `postponed` 3, `rescheduled` 2. **Treat any 1900 date as null.** The transform stores it as NULL, with a unit test (Phase 4).
- **Blind spot:** we only fetch shows happening in the next 90 days. Onsales for later shows are invisible to us, and the API's onsale filter doesn't select by public start (docs-vs-reality #4).

## 10. Errors

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
   It doesn't echo the key it was given.

- **No rate-limit headers** on any 400 or 401 response.
- **Not observed:** 403, 404, 429, 5xx, `Retry-After`.

### 429 codes the client relies on

Neither 429 code has been observed: we have never received a 429.

| Fault code (`fault.detail.errorcode`) | Meaning | Client action | Source | Observed? |
|---|---|---|---|---|
| `policies.ratelimit.QuotaViolation` | Daily quota (5,000) used up | Stop the run cleanly (`BudgetExhausted`) | **Documented** by Ticketmaster (Getting Started → Rate Limits), with the example body below | No |
| `policies.ratelimit.SpikeArrestViolation` | Per-second limit hit | Retry with backoff | **Documented** by Apigee, the gateway behind the API. Not in Ticketmaster's docs. | No |
| no code, or one we don't recognise | Unknown | Stop if the budget estimate is 10 or less (or the 429 itself reports `Rate-Limit-Available` ≤ 10); otherwise retry | our rule | — |
| `oauth.v2.InvalidApiKey` (401) | Bad key | Fail fast | Documented and **observed** (§10, item 2) | Yes |

Ticketmaster's documented quota body **contains the API key**:

```json
{"fault": {"faultstring": "Rate limit quota violation. Quota limit exceeded. Identifier : {apikey}", "detail": {"errorcode": "policies.ratelimit.QuotaViolation"}}}
```

So error text from any response is redacted before it goes into a message or a log.

Sources:
- [Ticketmaster Getting Started](https://developer.ticketmaster.com/products-and-docs/apis/getting-started/)
- [Apigee community: SpikeArrest error code](https://community.apigee.com/gc/Apigee/Error-code-that-Spike-arrest-policy-returns-by-Apigee/m-p/26640)
- [Apigee: Add the SpikeArrest policy](https://docs.cloud.google.com/apigee/docs/api-platform/tutorials/add-spike-arrest)

## 11. Is the API key in responses?

| Scan | Result |
|---|---|
| 39 responses in the first probe (including 400 and 401 bodies), body and every header | 0 contain the key |
| All 12,147 `_links` `href`s | 0 contain the key; 0 contain the text "apikey" |
| Later DIS1035 and DIS1024 bodies, printed in full | no key |
| Every file saved in `local/` | key absent |

`href`s are relative and echo our other parameters, e.g. `/discovery/v2/events.json?startDateTime=2026-10-06T15%3A06%3A52Z&dmaId=324&classificationName=music&endDateTime=2027-01-04T15%3A06%3A52Z&page=1&size=200&sort=date,asc`.

**Result: Phase 4 can store response bodies as received.** We still strip `apikey` from the `params` we save.

## 12. Rate limits

- **Headers on every 200 response:**

| Header | Value |
|---|---|
| `Rate-Limit` | `5000` |
| `Rate-Limit-Available` | e.g. `4997` |
| `Rate-Limit-Over` | `0` |
| `Rate-Limit-Reset` | `1791385613158` (epoch milliseconds) |

- **Reset time:** 2026-10-07T15:06:53.158Z, 24 hours after the day's first call (15:06:52Z). It didn't change across the day's calls.
- **400s count against the quota.**
  - The first probe opened with two 400s, and the first 200 showed 4,997.
  - Later, after the call that showed 4,964 came two 400s and one 401 sent with a fake key (which can't count against our key). The next 200 showed 4,961: the two 400s plus itself.
  - Because 400s carry no headers, **the budget guard must count errors itself.**
- **Used today:** 122 calls (5,000 → 4,878).
- **Per-second limit:** 5 per second (docs). Not tested: we paced every call at least 300 ms apart and saw no 429s. `Rate-Limit-Over` stayed at `0`.
- **Latency:** a full 200-event page took 0.57–0.95 s; a `size=1` call took 0.10–0.32 s.

## 13. IDs

| Entity | Shape (observed) |
|---|---|
| Event `id` | 13–21 chars, `[A-Za-z0-9_-]`, case-sensitive, mixed prefixes (`vv` 152, `Z7` 40, `Lv` 5, `ZF` 2, `1A` 1 of 200) |
| Venue `id` | 10–12 chars (prefixes `Kov`, `ZFr`, `Z6r`) |
| Attraction `id` | 10–12 chars (prefixes `K8v`, `Z7r`) |
| Music segment | `KZFzniwnSyZfZ7v7nJ` |

- **Uniqueness:** event IDs were unique within every page and across full sweeps (0 duplicates). Store them as `text` and compare case-sensitively.
- **The event `url` often doesn't contain the API `id`:** 149 of 200 end in a 16-character hex ID, 40 of 200 end in the API ID, and the rest vary. **Never parse IDs out of URLs.**
- **Venues and attractions per event:** every event had exactly 1 venue (1,200 of 1,200). Attractions ranged from 0 to 7.

## 14. Fields that are sometimes missing

"Sample" is `local/sample_events.json`: page 0 of the first probe, 200 events. "Sweep" is all 1,200 events of the first probe's `date,asc` sweep. "—" means not counted.

| Field | Missing in sample | Missing in sweep | Notes |
|---|---|---|---|
| `priceRanges` | **200 / 200** | **1,200 / 1,200** | no face-value prices at all |
| `dates.timezone` | 40 / 200 | 258 / 1,200 | `venue.timezone` is never missing, so use it as the fallback |
| `dates.start.localTime`, `dateTime` | 0 / 200 | 4 / 1,200 | `noSpecificTime=true` |
| `_embedded.venues[0].location` | 2 / 200 | 13 / 1,200 | **`latitude` and `longitude` are JSON strings, not numbers** |
| `_embedded.attractions` | 1 / 200 | 15 / 1,200 | |
| `_embedded.venues` | 0 / 200 | 0 / 1,200 | always exactly one |
| `sales.public.startDateTime` | 0 / 200 | — | 4 of 1,276 missing in the final query; 1900 placeholders in §9 |
| `name`, `url`, `dates.start.localDate`, `dates.status.code`, `classifications[0].segment` and `.genre`, `venue.name`, `city.name`, `state.stateCode`, `timezone` | 0 / 200 | — | |

**Values to handle:**

- **`dates.status.code`** across the sweep: `onsale` 1,144, **`cancelled`** 27, `rescheduled` 20, `offsale` 9. `postponed` also appears in the undated calls.
- **Genre placeholders** are strings, not nulls: `"Other"` 125, `"Undefined"` 30 (sweep).
- **Venue city names can have stray whitespace**, so strip them.
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

## 15. Response size

| | Range |
|---|---|
| Full page (200 events), JSON | 1,602–2,912 KB, about **11.4 KB per event** |
| Same pages gzipped on the wire | 112–325 KB |

- 200 responses are gzipped and chunked, with no `Content-Length`.
- Most of the weight is optional detail: `images`, embedded venue and attraction objects, `linkMoreInfo` in 10 locales, `ticketTextLines`. No query parameter we found trims the fields.

## 16. Budgets (PLAN §4)

Measured on one real run of the final query: 13 windows (all ≤ 200 events, so one page each) plus the 2 undated calls.

| | Per run |
|---|---|
| API calls | **15** |
| Events | 1,295 |
| Raw JSON | **14.46 MB** |

| Schedule | Calls per day (budget 2,500) | Raw MB per day | Days that fit in 500 MB | 3-day retention needs |
|---|---|---|---|---|
| every 1 h | 360 ✅ | 347.1 | **1.44** | 1,041 MB, **2.1× over** |
| every 2 h | 180 ✅ | 173.6 | 2.88 | 521 MB, just over |
| every 3 h | 120 ✅ | 115.7 | 4.32 | 347 MB, fits |

**Calls fit at every schedule. Storage, measured on JSON text, doesn't fit hourly with 3-day retention.**

Postgres stores `jsonb` and compresses large values, so the real on-disk size is unknown until it's measured. For comparison, the same bytes gzip 8.6× smaller (first probe), but Postgres compresses less than gzip. Hourly with 3-day retention fits only if the on-disk size is **6.9 MB per run or less**.

**Agreed procedure:**

1. In Phase 4, measure `pg_total_relation_size('raw_responses')` per run.
2. If hourly with 3-day retention still doesn't fit, the options in order are:
   1. Every 3 hours.
   2. Store a raw page only when its content hash changed.
