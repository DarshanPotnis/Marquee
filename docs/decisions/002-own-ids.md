# 002: Our own IDs, plus (source, source_id)

**Status:** accepted, 2026-10-06 (Phase 1, migration `001`). Written up in Phase 8.

## Problem

Every row in the clean tables (venues, attractions, events) comes from Ticketmaster and has Ticketmaster's ID: an opaque string (in the synthetic test data, `evt00042` or `KovFAKE001`). We need a key that:
- makes re-running a job update rows, never duplicate them;
- keeps foreign keys (an event's venue, a change's event) stable and cheap;
- doesn't lock the schema to one marketplace. The desk this imitates watches several.

## Options

1. **Ticketmaster's ID as the primary key.** Simple, and the upsert key is the primary key. But every foreign key becomes a Ticketmaster string, and a second source with overlapping ID formats would need a redesign.
2. **A composite natural key, `(source, source_id)`, as the primary key.** Correct for any number of sources, but every foreign key becomes two columns, and every join carries both.
3. **Our own surrogate key (`bigserial`) as the primary key, plus `UNIQUE (source, source_id)`.** *(Chosen.)*

## Choice

- `venues`, `attractions` and `events` each have their own `bigserial` key (`venue_id`, `attraction_id`, `event_id`) and a `UNIQUE (source, source_id)` constraint, with `source = 'ticketmaster'`.
- **Every write is an upsert on the natural key:** `INSERT … ON CONFLICT (source, source_id) DO UPDATE`. Loading the same page twice gives identical counts (a Phase 4 test).
- **Foreign keys use our IDs:** `events.venue_id`, `event_attractions`, `event_changes.event_id`.
- **Anything compared across rebuilds uses the natural key.** `rebuild --verify` compares on `(source, source_id)` and content, never on our IDs, because a rebuild into a throwaway schema numbers its rows differently.
- **`event_changes` records a venue change as Ticketmaster's venue ID,** not ours. That stays readable in the change log and survives a rebuild. The dashboard turns it into the venue's name.

## Why

- **A second marketplace is new rows, not a redesign:** a different `source` value with its own IDs, in the same tables.
- **Idempotency lives in one constraint.** The upsert key is declared in the schema, not remembered in code.
- **Small, stable foreign keys.** A `bigint` join is cheap, and a change log row stays valid however Ticketmaster formats its IDs.

## What it costs

- **Two keys to keep straight.** Code must never compare our IDs across databases or schemas; the verify step is the place that would get it wrong, and it uses natural keys.
- **The same show on two marketplaces is still two rows.** Matching them would need a separate mapping table; the IDs alone can't do it.

## What would change my mind

- **Certainty that there will only ever be one source.** Then Ticketmaster's ID as the key would be simpler.
- **Cross-source matching becoming the main job.** Then a canonical `shows` table, with per-source listings pointing at it, would come first.
