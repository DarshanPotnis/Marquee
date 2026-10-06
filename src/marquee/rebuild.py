"""Rebuild clean tables from raw, with zero API calls (PLAN §2, design rule 1).

- rebuild(): replay retained raw, in run order, onto the live tables. The repair tool after a
  transform fix. Change detection is off, and event_changes is never touched: it is append-only
  history, and replaying would either re-detect changes already recorded or, from pruned raw,
  silently lose everything older than retention.
- verify(): replay into a throwaway schema and compare with the live tables on natural keys and
  content, not on our surrogate ids or updated_at. Reports differences; changes nothing live.

What a replay can't reproduce once raw is pruned (docs/decisions/001-raw-first.md): events last
seen before the oldest retained raw, their first_seen_run, and event_changes older than retention.
Plain rebuild leaves those rows as they are; verify reports them as differences.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import uuid
from dataclasses import dataclass

from psycopg import sql

from marquee.db import Connection, migrate
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


@dataclass(frozen=True)
class Verification:
    raw_pages: int
    runs: int
    diffs: tuple[TableDiff, ...]

    @property
    def ok(self) -> bool:
        return all(d.only_live == 0 and d.only_rebuilt == 0 for d in self.diffs)


def rebuild(conn: Connection) -> ReplaySummary:
    """Replay retained raw onto the live tables, in one transaction."""
    with conn.transaction():
        return _replay(conn, _current_schema(conn))


def verify(conn: Connection) -> Verification:
    """Rebuild into a throwaway schema, compare with the live tables, then drop it."""
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
    finally:
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(live)))
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(scratch)))
    return Verification(summary.raw_pages, summary.runs, diffs)


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
_COMPARED = {
    "venues": ("source_id", """SELECT source, source_id, name, city, state, timezone, latitude,
                                      longitude FROM {s}.venues"""),
    "attractions": ("source_id",
                    "SELECT source, source_id, name, segment, genre FROM {s}.attractions"),
    "events": ("source_id", """SELECT e.source, e.source_id, e.name, v.source_id AS venue,
                                      e.local_date, e.local_time, e.starts_at, e.status,
                                      e.segment, e.genre, e.public_sale_start, e.url,
                                      e.first_seen_run, e.last_seen_run
                               FROM {s}.events e LEFT JOIN {s}.venues v USING (venue_id)"""),
    "event_attractions": ("event || ' / ' || attraction",
                          """SELECT e.source_id AS event, a.source_id AS attraction, ea.position
                             FROM {s}.event_attractions ea
                             JOIN {s}.events e USING (event_id)
                             JOIN {s}.attractions a USING (attraction_id)"""),
}


def _compare(conn: Connection, table: str, live: str, scratch: str) -> TableDiff:
    key, select = _COMPARED[table]
    live_rows = sql.SQL(select).format(s=sql.Identifier(live))
    rebuilt_rows = sql.SQL(select).format(s=sql.Identifier(scratch))
    only_live = sql.SQL("({}) EXCEPT ({})").format(live_rows, rebuilt_rows)
    only_rebuilt = sql.SQL("({}) EXCEPT ({})").format(rebuilt_rows, live_rows)
    count = sql.SQL("SELECT count(*) FROM ({}) d")
    sample = sql.SQL("SELECT DISTINCT {} FROM (({}) UNION ALL ({})) d ORDER BY 1 LIMIT 5").format(
        sql.SQL(key), only_live, only_rebuilt)
    a = conn.execute(count.format(only_live)).fetchone()
    b = conn.execute(count.format(only_rebuilt)).fetchone()
    samples = tuple(str(r[0]) for r in conn.execute(sample).fetchall())
    return TableDiff(table, int(a[0]) if a else 0, int(b[0]) if b else 0, samples)


def _current_schema(conn: Connection) -> str:
    row = conn.execute("SELECT current_schema()").fetchone()
    if row is None or row[0] is None:
        raise RebuildError("no current schema")
    return str(row[0])
