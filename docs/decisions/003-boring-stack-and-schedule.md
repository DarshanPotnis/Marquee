# 003: Boring stack, hourly schedule, 3-day raw retention

**Status:** accepted, 2026-10-06. Schedule and retention confirmed (hourly, 3 days). Running on GitHub Actions since Phase 7 (`.github/workflows/ingest.yml`).

## Problem

We need to run ingest on a schedule, store raw within Neon's free plan (`neon.max_cluster_size` = 1 GB), and keep the moving parts few. Two budgets apply (PLAN §4):
- **API calls:** under 2,500 a day.
- **Raw storage:** under 500 MB.

## Stack

- A scheduler: GitHub Actions cron, hourly at minute 17 (Phase 7). Runs queue and never overlap, with a 10-minute timeout.
- Postgres (Neon).
- Streamlit for the dashboard.

There is no orchestrator, no queue and no warehouse.

**Written upgrade triggers:**
- **An orchestrator,** when jobs start depending on each other.
- **Monthly partitions,** when `event_changes` or raw queries pass a few seconds.
- **A warehouse,** when Postgres strains.

## The numbers (measured on production, 2026-10-06)

| Per run | |
|---|---|
| API calls | 15 when measured (13 windows + 2 undated); **16 since Phase 5**, which added one whole-range total for `unique_vs_total` |
| Raw on disk | 1.82 MB (14.60 MB as received, stored as exact gzip; decision 005) |
| Clean tables | ~1.9 MB in total; these grow with new events, not with runs |
| Duration | ~22 s |

| Schedule | Calls a day | Raw MB a day | Raw at 3-day retention | at 7 days | at 14 days (the PLAN maximum) | Days that fit in 500 MB |
|---|---|---|---|---|---|---|
| **Every hour** | **360** (384 at 16 calls) | **43.8** | **131 MB** | 306 MB | 613 MB (over) | 11.4 |
| Every 2 h | 180 | 21.9 | 66 MB | 153 MB | 306 MB | 22.8 |
| Every 3 h | 120 | 14.6 | 44 MB | 102 MB | 204 MB | 34.2 |

## Decision

**Run hourly, keep raw for 3 days.**

- **Calls:** 360 a day is 14% of the 2,500 budget and 7% of the quota. At 16 calls a run since Phase 5, it's 384: 15% and 8%.
- **Raw:** 131 MB is 26% of the 500 MB raw budget and 13% of Neon's 1 GB.
- **Retention stays at PLAN §8's default of 3 days.** That's long enough to cover a weekend for debugging and repairs, and short enough to respect Ticketmaster's terms on caching.
- **Neither fallback is needed** (every 3 hours, or storing only changed pages). The Phase 0 estimate for the same schedule was 1,041 MB of JSON text; storing exact gzip instead of `jsonb` is what brought it to 131 MB.

## Compute (Neon free plan: 100 CU-hours a month)

Our compute autoscales between 0.25 and 0.5 CU, and suspends after 5 minutes idle.

- **Per run:** about 22 s of work (measured on 3 production runs; the first GitHub Actions run took 2 s to migrate plus 20 s to ingest) plus the 5-minute idle tail before suspend, so about **5.4 min awake**.
- **Per month:** 720 runs × 5.4 min is about **64 hours awake**.
- **Cost:** **16 CU-hours at 0.25 CU, 32 at 0.5 CU**, which is 16–32% of the allowance.
- **What else uses compute:**
  - CI and local tests: their own Postgres, so nothing.
  - Dashboard visits: mostly inside windows when compute is already awake, so little.
  - Manual `rebuild --verify`: about 23 s plus the idle tail.

## Not yet measured

- **Neon's console storage figure: skipped by decision** (Phase 6). The SQL measurement (`pg_database_size` and per-table sizes, shown on the dashboard) is the record.
- **Neon's CU-hours used.** The figures above are an estimate; the console's usage page has the real number after a day of scheduled runs.
- **Table bloat in steady state.** Every run rewrites each event row (`last_seen_run`) and its attraction links. Autovacuum should hold the clean tables to a small multiple of their 1.9 MB. To be confirmed after Phase 7's scheduled runs.

## What would change my mind

- **Raw per run growing,** for example if the market grows past one page per window.
- **Neon's console figure being much larger** than the SQL size.
- **A need for longer rebuild coverage.** 7 days at hourly (306 MB) still fits, if Ticketmaster's terms allow.
