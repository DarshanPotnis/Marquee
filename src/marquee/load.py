"""Write raw responses and clean rows. I/O: every function takes an open connection.

Upserts are keyed on (source, source_id), so our own ids never change and re-running never
duplicates. updated_at only moves when a row's content actually changed. Writes are batched per
page (executemany), because every round trip to the database costs tens of milliseconds.
"""

from __future__ import annotations

import gzip
import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb

from marquee.changes import Change, Snapshot, detect_changes, fold
from marquee.db import Connection
from marquee.tm_client import EVENTS_PATH, Page
from marquee.transform import AttractionRow, EventRow, PageRows, VenueRow

SOURCE = "ticketmaster"


def save_raw(conn: Connection, run_id: int, page: Page) -> int:
    """Keep the response exactly as received: gzip of the bytes, their SHA-256 and size."""
    row = conn.execute(
        "INSERT INTO raw_responses"
        " (run_id, endpoint, params, http_status, body_gzip, body_sha256, body_bytes)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING raw_id",
        (run_id, EVENTS_PATH, Jsonb(page.params), page.http_status, gzip.compress(page.raw),
         hashlib.sha256(page.raw).hexdigest(), len(page.raw)),
    ).fetchone()
    assert row is not None
    return int(row[0])


@dataclass(frozen=True)
class LoadResult:
    events: int
    changes_detected: int  # before folding into changes already recorded in this run


def load_rows(
    conn: Connection, run_id: int, rows: PageRows, *, track_changes: bool = True
) -> LoadResult:
    """Upsert one page's rows. With track_changes, record what changed in tracked fields."""
    venue_ids = _upsert_venues(conn, rows.venues)
    attraction_ids = _upsert_attractions(conn, rows.attractions)
    source_ids = [e.source_id for e in rows.events]
    stored = _snapshots(conn, source_ids) if track_changes else {}
    _upsert_events(conn, run_id, rows.events, venue_ids)
    event_ids = _ids(conn, "events", "event_id", source_ids)
    _replace_attractions(conn, rows.events, event_ids, attraction_ids)
    detected = 0
    if track_changes:
        found = {event_ids[e.source_id]: detect_changes(stored.get(e.source_id), Snapshot.of(e))
                 for e in rows.events}
        detected = sum(len(c) for c in found.values())
        if detected:
            _record_changes(conn, run_id, {k: v for k, v in found.items() if v})
    return LoadResult(len(rows.events), detected)


def _upsert_venues(conn: Connection, venues: Sequence[VenueRow]) -> dict[str, int]:
    conn.cursor().executemany(
        """INSERT INTO venues (source, source_id, name, city, state, timezone, latitude, longitude)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (source, source_id) DO UPDATE SET
             name = EXCLUDED.name, city = EXCLUDED.city, state = EXCLUDED.state,
             timezone = EXCLUDED.timezone, latitude = EXCLUDED.latitude,
             longitude = EXCLUDED.longitude, updated_at = now()
           WHERE (venues.name, venues.city, venues.state, venues.timezone, venues.latitude,
                  venues.longitude)
             IS DISTINCT FROM (EXCLUDED.name, EXCLUDED.city, EXCLUDED.state, EXCLUDED.timezone,
                               EXCLUDED.latitude, EXCLUDED.longitude)""",
        [(SOURCE, v.source_id, v.name, v.city, v.state, v.timezone, v.latitude, v.longitude)
         for v in venues],
    )
    return _ids(conn, "venues", "venue_id", [v.source_id for v in venues])


def _upsert_attractions(conn: Connection, attractions: Sequence[AttractionRow]) -> dict[str, int]:
    conn.cursor().executemany(
        """INSERT INTO attractions (source, source_id, name, segment, genre)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (source, source_id) DO UPDATE SET
             name = EXCLUDED.name, segment = EXCLUDED.segment, genre = EXCLUDED.genre,
             updated_at = now()
           WHERE (attractions.name, attractions.segment, attractions.genre)
             IS DISTINCT FROM (EXCLUDED.name, EXCLUDED.segment, EXCLUDED.genre)""",
        [(SOURCE, a.source_id, a.name, a.segment, a.genre) for a in attractions],
    )
    return _ids(conn, "attractions", "attraction_id", [a.source_id for a in attractions])


_EVENT_CONTENT = ("name", "venue_id", "local_date", "local_time", "starts_at", "status", "segment",
                  "genre", "public_sale_start", "url")


def _upsert_events(
    conn: Connection, run_id: int, events: Sequence[EventRow], venue_ids: dict[str, int]
) -> None:
    cols = [sql.Identifier(c) for c in _EVENT_CONTENT]
    query = sql.SQL(
        """INSERT INTO events (source, source_id, {cols}, first_seen_run, last_seen_run)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (source, source_id) DO UPDATE SET
             {assignments},
             last_seen_run = GREATEST(events.last_seen_run, EXCLUDED.last_seen_run),
             updated_at = CASE WHEN ({stored}) IS DISTINCT FROM ({incoming})
                               THEN now() ELSE events.updated_at END"""
    ).format(
        cols=sql.SQL(", ").join(cols),
        assignments=sql.SQL(", ").join(sql.SQL("{c} = EXCLUDED.{c}").format(c=c) for c in cols),
        stored=sql.SQL(", ").join(sql.SQL("events.{}").format(c) for c in cols),
        incoming=sql.SQL(", ").join(sql.SQL("EXCLUDED.{}").format(c) for c in cols),
    )
    conn.cursor().executemany(
        query,
        [(SOURCE, e.source_id, e.name,
          venue_ids.get(e.venue_source_id) if e.venue_source_id else None,
          e.local_date, e.local_time, e.starts_at, e.status, e.segment, e.genre,
          e.public_sale_start, e.url, run_id, run_id) for e in events],
    )


def _replace_attractions(
    conn: Connection, events: Sequence[EventRow], event_ids: dict[str, int],
    attraction_ids: dict[str, int],
) -> None:
    conn.execute("DELETE FROM event_attractions WHERE event_id = ANY(%s)",
                 (list(event_ids.values()),))
    conn.cursor().executemany(
        "INSERT INTO event_attractions (event_id, attraction_id, position) VALUES (%s, %s, %s)",
        [(event_ids[e.source_id], attraction_ids[a], position)
         for e in events for position, a in enumerate(e.attraction_ids) if a in attraction_ids],
    )


def _snapshots(conn: Connection, source_ids: Sequence[str]) -> dict[str, Snapshot]:
    rows = conn.execute(
        """SELECT e.source_id, e.status, e.local_date, e.local_time, v.source_id,
                  e.public_sale_start
           FROM events e LEFT JOIN venues v ON v.venue_id = e.venue_id
           WHERE e.source = %s AND e.source_id = ANY(%s)""",
        (SOURCE, list(source_ids)),
    ).fetchall()
    return {r[0]: Snapshot(r[1], r[2], r[3], r[4], r[5]) for r in rows}


def _record_changes(
    conn: Connection, run_id: int, found: dict[int, list[Change]]
) -> None:
    # An event can be seen twice in one run (a probe page, then its child window). Fold a repeat
    # change into the run's existing row instead of colliding with its primary key.
    existing = {
        (r[0], r[1]): Change(r[1], r[2], r[3])
        for r in conn.execute(
            "SELECT event_id, field, old_value, new_value FROM event_changes"
            " WHERE run_id = %s AND event_id = ANY(%s)", (run_id, list(found))).fetchall()
    }
    upserts: list[tuple[Any, ...]] = []
    deletes: list[tuple[Any, ...]] = []
    for event_id, changes in found.items():
        for change in changes:
            merged = fold(existing.get((event_id, change.field)), change)
            if merged is None:
                deletes.append((event_id, run_id, change.field))
            else:
                upserts.append((event_id, run_id, merged.field, merged.old, merged.new))
    conn.cursor().executemany(
        """INSERT INTO event_changes (event_id, run_id, detected_at, field, old_value, new_value)
           VALUES (%s, %s, now(), %s, %s, %s)
           ON CONFLICT (event_id, run_id, field) DO UPDATE SET
             old_value = EXCLUDED.old_value, new_value = EXCLUDED.new_value,
             detected_at = EXCLUDED.detected_at""",
        upserts,
    )
    conn.cursor().executemany(
        "DELETE FROM event_changes WHERE event_id = %s AND run_id = %s AND field = %s", deletes
    )


def _ids(conn: Connection, table: str, id_column: str, source_ids: Iterable[str]) -> dict[str, int]:
    query = sql.SQL("SELECT source_id, {} FROM {} WHERE source = %s AND source_id = ANY(%s)")
    rows = conn.execute(
        query.format(sql.Identifier(id_column), sql.Identifier(table)), (SOURCE, list(source_ids))
    ).fetchall()
    return {r[0]: r[1] for r in rows}
