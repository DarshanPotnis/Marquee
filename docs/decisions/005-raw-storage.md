# 005: Store raw responses as gzip of the exact bytes received

**Status:** accepted, 2026-10-06 (Phase 4). Implemented by migration `002`.

## Problem

Raw responses are the source of truth: the clean tables are rebuilt from them. That requires two things.

1. **Exact.** We should be able to show precisely what the API sent.
2. **Small.** One run is 15 responses and **14.60 MB** of JSON. At hourly runs that's 350 MB a day of text, against a 500 MB raw budget on Neon's 1 GB plan.

## Options, measured

On one real run (15 responses), on the Neon dev branch, each stored three ways:

| Option | On disk | Read back + parse | Byte-exact? |
|---|---|---|---|
| `jsonb` (Neon's default pglz compression) | 6.32 MB | 1.25 s | 0 of 15 (reorders keys, drops whitespace) |
| `jsonb` with lz4 | 3.58 MB | 1.01 s | 0 of 15 |
| **gzip of the exact bytes, `bytea` (`STORAGE EXTERNAL`) + SHA-256 + size** | **1.84 MB** | **0.44 s** | **15 of 15**; all hashes match |

Compressing a whole run with Python's standard-library gzip took 0.20 s. No new dependency.

## Choice

`raw_responses` holds three columns for the body:
- `body_gzip`: gzip of the body exactly as received.
- `body_sha256`: the SHA-256 of the uncompressed bytes.
- `body_bytes`: the uncompressed size.

Storage is set to `EXTERNAL`, because the data is already compressed. `jsonb` is gone.

## Why

- **The only exact option.** `jsonb` normalises JSON, so it can't show what the API actually sent.
- **3.4× smaller than `jsonb`,** and fastest to replay.
- **Rebuild checks integrity.** It refuses any body whose bytes don't match its hash.
- **The hash also enables a fallback** we may need later: store a page only when its content changed.

## What we give up

SQL can't query inside raw (no `->>` operators). That's acceptable: raw exists to be replayed, and the clean tables are what people query. Ad-hoc digging into raw goes through Python, by decompressing and parsing.

## Confirmed in production

Two real runs stored **1.82 MB per run on disk** (14.60 MB as received; 1.69 MB of gzip plus storage overhead), matching the measurement.

## What would change my mind

- **A need to query raw in SQL regularly.** Then `jsonb` with lz4, at 3.58 MB per run, still fits the budget.
- **Neon offering zstd column compression** that's both exact and as small.
- **Storage getting tight anyway.** Then store a page only when its `body_sha256` differs from the previous run's.
