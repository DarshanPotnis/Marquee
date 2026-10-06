-- 003: check severity, and run counters for checks and prune (Phase 5). Never edit after it has run
-- anywhere; add 004_*.sql.
--
-- Severity separates "the run's data can't be trusted" (error: ingest exits non-zero, the scheduled
-- run turns red and its owner is notified) from "something upstream looks off" (warning: shown on
-- the dashboard). `passed` keeps meaning "the condition held"; severity says how much it matters.

ALTER TABLE check_results
  ADD COLUMN severity text NOT NULL DEFAULT 'error' CHECK (severity IN ('error', 'warning'));

ALTER TABLE ingest_runs
  ADD COLUMN checks_failed int,   -- error-level checks that failed in this run
  ADD COLUMN pruned_raw    int;   -- raw responses deleted by this run's prune
