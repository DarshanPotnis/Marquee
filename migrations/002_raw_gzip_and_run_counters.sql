-- 002: raw stored as gzip of the exact bytes received; run counters for probes, undated calls and
-- implausible onsale dates. Never edit after it has run anywhere; add 003_*.sql.
--
-- Decided by measurement (docs/decisions/005-raw-storage.md). One real run, 15 responses, 14.60 MB
-- of JSON as received, took 6.32 MB as jsonb, 3.58 MB as jsonb with lz4, and 1.84 MB as gzip
-- bytea. Only gzip bytea gave the exact bytes back (15 of 15; jsonb 0 of 15: it reorders keys and
-- drops whitespace).
--
-- raw_responses is empty when this runs (nothing was ingested before Phase 4), so the NOT NULL
-- columns need no default. On a table with rows this would fail loudly, which is intended.

ALTER TABLE raw_responses DROP COLUMN body;
ALTER TABLE raw_responses
  ADD COLUMN body_gzip   bytea NOT NULL,   -- gzip of the response body exactly as received
  ADD COLUMN body_sha256 text  NOT NULL,   -- SHA-256 of the uncompressed bytes
  ADD COLUMN body_bytes  int   NOT NULL;   -- uncompressed size in bytes
-- Already compressed: don't let Postgres spend time trying to compress it again.
ALTER TABLE raw_responses ALTER COLUMN body_gzip SET STORAGE EXTERNAL;

ALTER TABLE ingest_runs
  ADD COLUMN probe_calls    int,    -- page-0 calls for windows that were then split
  ADD COLUMN probe_events   int,    -- events on those pages; they reappear in the child windows
  ADD COLUMN undated_events int,    -- events from the two undated (TBA/TBD) calls
  ADD COLUMN onsale_nulled  jsonb;  -- implausible public onsale dates set to NULL, by reason
