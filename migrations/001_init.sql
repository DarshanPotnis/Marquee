-- 001_init: the whole schema from docs/PLAN.md §3.
-- Applied once by `python -m marquee migrate`. Never edit after it has run anywhere; add 002_*.sql.

CREATE TABLE ingest_runs (
  run_id          bigserial PRIMARY KEY,
  source          text        NOT NULL,                  -- 'ticketmaster'
  started_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz,
  status          text        NOT NULL DEFAULT 'running', -- running | succeeded | partial | failed
  windows         int,
  api_calls       int         NOT NULL DEFAULT 0,
  reported_total  int,        -- sum of page.totalElements across windows
  fetched_total   int,        -- events received across windows (boundary overlaps included)
  unique_events   int,        -- distinct events after dedupe
  budget_left     int,        -- last Rate-Limit-Available seen
  error           text
);

CREATE TABLE raw_responses (
  raw_id       bigserial PRIMARY KEY,
  run_id       bigint      NOT NULL REFERENCES ingest_runs,
  fetched_at   timestamptz NOT NULL DEFAULT now(),
  endpoint     text        NOT NULL,
  params       jsonb       NOT NULL,   -- NEVER contains the API key
  http_status  int         NOT NULL,
  body         jsonb       NOT NULL
);
CREATE INDEX ON raw_responses (fetched_at);

CREATE TABLE venues (
  venue_id    bigserial PRIMARY KEY,
  source      text NOT NULL,
  source_id   text NOT NULL,
  name        text NOT NULL,
  city        text,
  state       text,
  timezone    text,
  latitude    double precision,   -- arrives as a JSON string; parsed
  longitude   double precision,   -- arrives as a JSON string; parsed
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, source_id)
);

CREATE TABLE attractions (              -- artists, bands, shows
  attraction_id bigserial PRIMARY KEY,
  source        text NOT NULL,
  source_id     text NOT NULL,
  name          text NOT NULL,
  segment       text,
  genre         text,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, source_id)
);

CREATE TABLE events (
  event_id          bigserial PRIMARY KEY,
  source            text NOT NULL,
  source_id         text NOT NULL,
  name              text NOT NULL,
  venue_id          bigint REFERENCES venues,
  local_date        date,
  local_time        time,               -- null when time is TBA
  starts_at         timestamptz,        -- null when date or time is TBA/TBD
  status            text,               -- as the API spells it: onsale | offsale | cancelled | postponed | rescheduled
  segment           text,
  genre             text,
  public_sale_start timestamptz,        -- null when missing or the 1900-01-01 placeholder
  url               text,
  first_seen_run    bigint REFERENCES ingest_runs,
  last_seen_run     bigint REFERENCES ingest_runs,
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, source_id)
);
CREATE INDEX ON events (local_date);

CREATE TABLE event_attractions (
  event_id      bigint   NOT NULL REFERENCES events ON DELETE CASCADE,
  attraction_id bigint   NOT NULL REFERENCES attractions,
  position      smallint NOT NULL,      -- 0 = headliner (first listed)
  PRIMARY KEY (event_id, attraction_id)
);

-- Append-only. One row per tracked field that differs between a re-fetched event and what we stored.
CREATE TABLE event_changes (
  event_id    bigint      NOT NULL REFERENCES events,
  run_id      bigint      NOT NULL REFERENCES ingest_runs,
  detected_at timestamptz NOT NULL,
  field       text        NOT NULL CHECK (field IN
                ('status', 'local_date', 'local_time', 'venue', 'public_sale_start')),
  old_value   text,                     -- null when the stored value was null
  new_value   text,                     -- null when the value went away (e.g. a show became TBA)
  PRIMARY KEY (event_id, run_id, field)
);
CREATE INDEX ON event_changes (detected_at);

CREATE TABLE check_results (
  run_id     bigint  NOT NULL REFERENCES ingest_runs,
  check_name text    NOT NULL,
  passed     boolean NOT NULL,
  observed   numeric,
  expected   numeric,
  detail     text,
  PRIMARY KEY (run_id, check_name)
);
