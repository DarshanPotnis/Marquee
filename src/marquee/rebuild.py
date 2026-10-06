"""Rebuild clean tables from raw, with zero API calls (PLAN §2, design rule 1).

- rebuild(): replay retained raw, in run order, onto the live tables. The repair tool after a
  transform fix. Change detection is off, and event_changes is never touched: it is append-only
  history, and replaying would either re-detect changes already recorded or, from pruned raw,
  silently lose everything older than retention.
- verify(): replay into a throwaway schema and compare with the live tables on natural keys and
  content, not on our surrogate ids or updated_at. Reports differences; changes nothing live.

What a replay can't reproduce once raw is pruned (docs/decisions/001-raw-first.md): events last
seen before the oldest retained raw (with their venues, attractions and links), the
first_seen_run of events first seen then, and event_changes older than retention. Plain rebuild
leaves those rows as they are; verify compares only what retained raw can reproduce and reports
the rest as "not reproducible (pruned)", not as differences.

Both take the ingest lock, so a scheduled run can't write between replayed pages, and ingest
stands aside while they run.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import uuid
from dataclasses import dataclass

from psycopg import sql

from marquee.db import INGEST_LOCK, Connection, advisory_lock, migrate
from marquee.load import load_rows
from marquee.transform import transform_page


class RebuildError(RuntimeError):
    """Raw data can't be trusted (it doesn't match its hash); nothing was rebuilt."""


@dataclass(frozen=True)
class ReplaySummary:
    raw_pages: int
    runs: int


@dataclass(frozen=True)
class TableDiff:
    table: str
    only_live: int  # rows in the live table with no identical rebuilt row
    only_rebuilt: int  # rebuilt rows with no identical live row
    samples: tuple[str, ...]  # natural keys of a few differing rows
    pruned: int = 0  # live rows only pruned raw could reproduce: reported, not compared


@dataclass(frozen=True)
class Verification:
    raw_pages: int
    runs: int
    diffs: tuple[TableDiff, ...]
    first_seen_pruned: int = 0  # events whose first_seen_run is a run with no raw left

    @property
    def ok(self) -> bool:
        return all(d.only_live == 0 and d.only_rebuilt == 0 for d in self.diffs)


def rebuild(conn: Connection) -> ReplaySummary:
    """Replay retained raw onto the live tables, in one transaction, holding the ingest lock."""
    with advisory_lock(conn, INGEST_LOCK), conn.transaction():
        return _replay(conn, _current_schema(conn))


def verify(conn: Connection) -> Verification:
    """Rebuild into a throwaway schema, compare with the live tables, then drop it."""
    with advisory_lock(conn, INGEST_LOCK):
        return _verify(conn)


def _verify(conn: Connection) -> Verification:
    live = _current_schema(conn)
    scratch = f"marquee_verify_{uuid.uuid4().hex[:12]}"
    conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(scratch)))
    try:
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(scratch)))
        migrate(conn)  # the same DDL as live, with its own sequences
        # Runs are copied so first_seen_run and last_seen_run have something to reference.
        conn.execute(sql.SQL("INSERT INTO ingest_runs SELECT * FROM {}.ingest_runs").format(
            sql.Identifier(live)))
        with conn.transaction():
            summary = _replay(conn, live)  # reads live raw, writes the scratch tables
        diffs = tuple(_compare(conn, table, live, scratch) for table in _COMPARED)
        first_seen = conn.execute(sql.SQL(_FIRST_SEEN_PRUNED).format(
            live=sql.Identifier(live))).fetchone()
    finally:
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(live)))
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(scratch)))
    return Verification(summary.raw_pages, summary.runs, diffs,
                        int(first_seen[0]) if first_seen else 0)


def _replay(conn: Connection, raw_schema: str) -> ReplaySummary:
    schema = sql.Identifier(raw_schema)
    pages = conn.execute(sql.SQL(
        """SELECT r.raw_id, r.run_id, r.body_sha256, i.started_at
           FROM {}.raw_responses r JOIN {}.ingest_runs i USING (run_id)
           ORDER BY r.run_id, r.raw_id""").format(schema, schema)).fetchall()
    fetch_body = sql.SQL("SELECT body_gzip FROM {}.raw_responses WHERE raw_id = %s").format(schema)
    for raw_id, run_id, sha256, started_at in pages:
        row = conn.execute(fetch_body, (raw_id,)).fetchone()
        body = gzip.decompress(row[0]) if row else b""
        if hashlib.sha256(body).hexdigest() != sha256:
            raise RebuildError(f"raw {raw_id} doesn't match its SHA-256; refusing to rebuild")
        # The run's own start time, exactly as ingest used it, so the onsale rule replays the same.
        rows = transform_page(json.loads(body), run_at=started_at)
        load_rows(conn, run_id, rows, track_changes=False)
    return ReplaySummary(len(pages), len({p[1] for p in pages}))


# Natural keys and content only: never our surrogate ids or updated_at, which differ by design.
# Scope: what retained raw can reproduce. A live event is in scope if its last sighting's raw is
# retained ({retained}); venues and attractions if an in-scope event uses them. Each side keeps
# its in-scope rows plus any row live doesn't have at all, so a rebuilt row that shouldn't exist
# is still a difference. first_seen_run is compared only where its own run's raw is retained.
_RETAINED = "SELECT DISTINCT run_id FROM {live}.raw_responses"
_IN_SCOPE_EVENTS = f"SELECT event_id FROM {{live}}.events WHERE last_seen_run IN ({_RETAINED})"
_IN_SCOPE_VENUES = (f"SELECT venue_id FROM {{live}}.events WHERE venue_id IS NOT NULL"
                    f" AND event_id IN ({_IN_SCOPE_EVENTS})")
_IN_SCOPE_ATTRACTIONS = (f"SELECT attraction_id FROM {{live}}.event_attractions"
                         f" WHERE event_id IN ({_IN_SCOPE_EVENTS})")
_COMPARED = {
    "venues": (
        "source_id",
        f"""SELECT t.source, t.source_id, t.name, t.city, t.state, t.timezone, t.latitude,
                   t.longitude
            FROM {{s}}.venues t
            LEFT JOIN {{live}}.venues l ON l.source = t.source AND l.source_id = t.source_id
            WHERE l.venue_id IS NULL OR l.venue_id IN ({_IN_SCOPE_VENUES})""",
        f"SELECT count(*) FROM {{live}}.venues WHERE venue_id NOT IN ({_IN_SCOPE_VENUES})"),
    "attractions": (
        "source_id",
        f"""SELECT t.source, t.source_id, t.name, t.segment, t.genre
            FROM {{s}}.attractions t
            LEFT JOIN {{live}}.attractions l
                   ON l.source = t.source AND l.source_id = t.source_id
            WHERE l.attraction_id IS NULL OR l.attraction_id IN ({_IN_SCOPE_ATTRACTIONS})""",
        f"""SELECT count(*) FROM {{live}}.attractions
            WHERE attraction_id NOT IN ({_IN_SCOPE_ATTRACTIONS})"""),
    "events": (
        "source_id",
        f"""SELECT e.source, e.source_id, e.name, v.source_id AS venue, e.local_date,
                   e.local_time, e.starts_at, e.status, e.segment, e.genre, e.public_sale_start,
                   e.url,
                   CASE WHEN l.event_id IS NULL OR l.first_seen_run IN ({_RETAINED})
                        THEN e.first_seen_run END AS first_seen_run,
                   e.last_seen_run
            FROM {{s}}.events e LEFT JOIN {{s}}.venues v ON v.venue_id = e.venue_id
            LEFT JOIN {{live}}.events l ON l.source = e.source AND l.source_id = e.source_id
            WHERE l.event_id IS NULL OR l.event_id IN ({_IN_SCOPE_EVENTS})""",
        f"SELECT count(*) FROM {{live}}.events WHERE event_id NOT IN ({_IN_SCOPE_EVENTS})"),
    "event_attractions": (
        "event || ' / ' || attraction",
        f"""SELECT e.source_id AS event, a.source_id AS attraction, ea.position
            FROM {{s}}.event_attractions ea
            JOIN {{s}}.events e ON e.event_id = ea.event_id
            JOIN {{s}}.attractions a ON a.attraction_id = ea.attraction_id
            LEFT JOIN {{live}}.events l ON l.source = e.source AND l.source_id = e.source_id
            WHERE l.event_id IS NULL OR l.event_id IN ({_IN_SCOPE_EVENTS})""",
        f"""SELECT count(*) FROM {{live}}.event_attractions
            WHERE event_id NOT IN ({_IN_SCOPE_EVENTS})"""),
}
_FIRST_SEEN_PRUNED = (f"SELECT count(*) FROM {{live}}.events WHERE event_id IN ({_IN_SCOPE_EVENTS})"
                      f" AND first_seen_run NOT IN ({_RETAINED})")


def _compare(conn: Connection, table: str, live: str, scratch: str) -> TableDiff:
    key, select, pruned_count = _COMPARED[table]
    live_id = sql.Identifier(live)
    live_rows = sql.SQL(select).format(s=live_id, live=live_id)
    rebuilt_rows = sql.SQL(select).format(s=sql.Identifier(scratch), live=live_id)
    only_live = sql.SQL("({}) EXCEPT ({})").format(live_rows, rebuilt_rows)
    only_rebuilt = sql.SQL("({}) EXCEPT ({})").format(rebuilt_rows, live_rows)
    count = sql.SQL("SELECT count(*) FROM ({}) d")
    sample = sql.SQL("SELECT DISTINCT {} FROM (({}) UNION ALL ({})) d ORDER BY 1 LIMIT 5").format(
        sql.SQL(key), only_live, only_rebuilt)
    a = conn.execute(count.format(only_live)).fetchone()
    b = conn.execute(count.format(only_rebuilt)).fetchone()
    c = conn.execute(sql.SQL(pruned_count).format(live=live_id)).fetchone()
    samples = tuple(str(r[0]) for r in conn.execute(sample).fetchall())
    return TableDiff(table, int(a[0]) if a else 0, int(b[0]) if b else 0, samples,
                     int(c[0]) if c else 0)


def _current_schema(conn: Connection) -> str:
    row = conn.execute("SELECT current_schema()").fetchone()
    if row is None or row[0] is None:
        raise RebuildError("no current schema")
    return str(row[0])
