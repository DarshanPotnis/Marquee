# 004: Adaptive windows in Los Angeles calendar days

**Status:** accepted, 2026-10-06 (Phase 3).

## Problem

The Discovery API only lets one search page so deep. At `size=200` it serves pages 0–5, so **1,200 events at most**, and page 6 returns HTTP 400 `DIS1035`. The docs say the limit is 1,000.

On 2026-10-06, the next 90 days of Ticketmaster LA market Music held **1,282** events. One search, paged to the end, fetched 1,200 and **lost 82**. We need every event, and we need to *prove* we got every event.

## Options

1. **One search, paged to the end** (brute force). Simple, but it loses everything past item 1,200, and the loss grows with the market.
2. **Fixed small windows**, such as one per day. Always under the cap, but always 90+ calls per run even when a week holds 120 events.
3. **Weekly windows, halved while page 0 reports more than 1,000**, down to one day. *(Chosen.)*
4. **The API's `localStartDateTime` filter**, to window by local time directly. Its format isn't documented, and we haven't tested it.

## Choice

- Weekly windows over the range from now to the Los Angeles midnight 90 days out.
- Every boundary after the first is a **Los Angeles local midnight**, converted to UTC for the API.
- A window whose page 0 reports more than **1,000** is halved at a local midnight, down to one local day.
- A day still over 1,000 is fetched as far as the API allows and flagged `over_cap`.
- Windows **share edges**, because the API includes both edges.
- The run's numbers come from the **final** windows. Probe pages are counted separately.

## Why

- **It costs nothing extra today.** The busiest week holds about 165 events, so the 90 days take 13 calls, one per window. That's 6 more than brute force, and nothing is lost.
- **The threshold is 1,000, not the observed 1,200**, because 1,000 is what both the docs and the error message say. If Ticketmaster fixes its off-by-one, a 1,200 threshold would start failing busy windows.
- **Local days, not UTC days**:
  - A show "on Nov 1" is on the Los Angeles Nov 1, which is 25 hours long (daylight saving ends).
  - Events with no specific time sit inside their own local day (api-notes §7).
  - Splitting at local midnights keeps each day whole.
- **Shared edges are safe.** We tested against the real API: an event exactly on an edge is returned by both windows. So there's no gap, only a harmless duplicate that the dedupe removes.
- **Completeness is checked, not assumed.** The run compares unique event IDs across all windows with the API's reported total for the whole range at that moment. On 2026-10-06: 1,282 = 1,282.

## What would change my mind

- **A single local day regularly over 1,000 events.** We'd need a finer split than a day (by hour, or by venue) or a second filter dimension.
- **Ticketmaster documenting `localStartDateTime` with a clear format and edge rules.** Windowing in local time directly would remove the UTC conversion.
- **The edge test failing on a later probe**, for example an edge turning exclusive. Then each window's end would move 1 s later; overlaps are harmless, gaps aren't.
- **Call budget becoming tight.** Then we'd try wider starting windows (two weeks) before splitting.
