# 001: Raw first; rebuild replays raw and never touches event_changes

**Status:** accepted, 2026-10-06 (Phase 4).

## Problem

Parsing bugs happen. When one is fixed, the clean tables have to be corrected without fetching everything again, which would spend quota and lose the past. We also need to show that the clean tables really follow from what the API sent.

But raw data is kept only for `RAW_RETENTION_DAYS` (Ticketmaster's terms; PLAN §8). So a rebuild can only ever see recent raw.

## Options

1. **Load clean tables straight from the API, and keep no raw.** Fixing a bug means a new fetch, and history is lost.
2. **Keep raw; rebuild by emptying the clean tables and reloading from raw.** `event_changes` points at our `event_id`. Emptying `events` would cascade-delete that history and renumber our IDs. And once raw is pruned, a reload can't bring back anything older.
3. **Keep raw; rebuild by replaying it onto the existing tables.** Replay in run order, through the same transform and upserts. Change detection is off, and `event_changes` is never touched. *(Chosen.)*

Option 3 comes with a separate check, `rebuild --verify`, which rebuilds into a throwaway schema and compares it with the live tables.

## Choice

- **Every page goes to raw first**, as gzip of the exact bytes (decision 005). The transform reads only raw.
- **`rebuild`** replays retained raw in run order (run, then raw ID) onto the live tables, in one transaction, with **zero API calls**.
  - It uses each run's own start time, so time-dependent rules (the onsale plausibility check) replay exactly.
  - It is the repair tool after a transform fix.
- **`rebuild --verify`** rebuilds into a throwaway schema and compares with the live tables.
  - It matches on `(source, source_id)` and content columns, never our surrogate IDs or `updated_at`.
  - It reports differences, drops the scratch schema, and changes nothing live.
  - This is the real proof. Replaying onto tables that already hold the same data would come out identical almost by construction, which proves very little.
- **`event_changes` is append-only history,** and rebuild never touches it.

## What rebuild can and can't reproduce

While all raw is retained, the clean tables come out **identical**. Real check, 2026-10-06: 30 raw pages from 2 runs, 0 differences in venues, attractions, events and event attractions.

Once raw has been pruned:

| | Can rebuild reproduce it? | What happens |
|---|---|---|
| Events seen in retained raw | Yes, every field | Refreshed from raw |
| Events last seen before the oldest retained raw | No: no raw left | Left as they are, not deleted. `--verify` reports them as only-live. |
| `first_seen_run` of events first seen before the oldest retained raw | No | Kept as stored. Rebuild never moves it. `--verify` reports the difference. |
| `event_changes` older than retention | No | Never regenerated, never deleted. It is the only record of that history. |
| `event_changes` within retention | Could be, but isn't | Replaying would re-detect changes already recorded, so change detection is off during rebuild. |

A test proves the pruning case: after deleting a run's raw, rebuild leaves the vanished event and its `first_seen_run` alone, and `--verify` reports both differences.

## What would change my mind

- **Retention long enough to cover the whole history we care about.** Then a from-scratch rebuild into new tables, swapped in atomically, becomes attractive.
- **Needing `event_changes` reproducible on its own.** Then record changes from raw in a separate, re-derivable table, and keep the current one as the audit log.
