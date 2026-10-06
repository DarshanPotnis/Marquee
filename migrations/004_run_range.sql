-- 004: the range each run covered. Never edit after it has run anywhere; add 005_*.sql.
--
-- The window moves forward every LA midnight, so each day a new day of shows appears for the first
-- time without having just been listed. The dashboard tells them apart from newly listed shows by
-- comparing with the previous run's range, so each run keeps its own.
--
-- Runs from before 004 are backfilled with the range plan_range gives for their start: from that
-- second to the LA midnight that ends the 90th day, counting the LA day the run started on. Ingest
-- reads its clock milliseconds before the row is opened, so these are within a second of the truth.
-- NULL only for a run opened outside ingest.

ALTER TABLE ingest_runs
  ADD COLUMN range_start timestamptz,   -- the run's "now", to the second
  ADD COLUMN range_end   timestamptz;   -- LA midnight after the last day; the API includes it

UPDATE ingest_runs
SET range_start = date_trunc('second', started_at),
    range_end = (((started_at AT TIME ZONE 'America/Los_Angeles')::date + 90)::timestamp
                 AT TIME ZONE 'America/Los_Angeles');
