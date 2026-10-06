"""Read queries for the dashboard. Every SQL statement the dashboard runs lives here.

Connections are read-only from the moment they open (make_readonly), with a statement timeout, so
the dashboard can never change data and one slow query can't hang the page. All queries are
parameterised. "Currently listed" means seen in the latest succeeded run: a show the API stopped
returning drops off the upcoming views.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time

from marquee import db
from marquee.db import Connection

STATEMENT_TIMEOUT = "5s"
_LISTED = "e.last_seen_run >= (SELECT max(run_id) FROM ingest_runs WHERE status = 'succeeded')"


@dataclass(frozen=True)
class RunRow:
    run_id: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    api_calls: int
    reported_total: int | None
    fetched_total: int | None
    unique_events: int | None
    undated_events: int | None
    changes: int
    checks_failed: int | None
    pruned_raw: int | None
    error: str | None


@dataclass(frozen=True)
class Completeness:
    run_id: int
    finished_at: datetime
    reported: int  # the API's total for the whole range
    received: int  # unique event ids across the windows
    undated: int  # TBA/TBD events, outside the date windows


@dataclass(frozen=True)
class CheckRow:
    name: str
    passed: bool
    severity: str
    detail: str | None


@dataclass(frozen=True)
class ChangeRow:
    detected_at: datetime
    show: str
    venue: str | None
    event_date: date | None  # the event's date now
    field: str
    old: str | None  # venue changes are shown as venue names
    new: str | None


@dataclass(frozen=True)
class NewShow:
    first_seen_at: datetime
    show: str
    venue: str | None
    event_date: date | None
    event_time: time | None


@dataclass(frozen=True)
class Onsale:
    onsale_at: datetime
    show: str
    venue: str | None
    event_date: date | None


@dataclass(frozen=True)
class EventRow:
    show: str
    venue: str | None
    city: str | None
    event_date: date | None
    event_time: time | None
    status: str | None
    onsale_at: datetime | None


@dataclass(frozen=True)
class Storage:
    database_bytes: int
    limit_bytes: int | None  # Neon's neon.max_cluster_size; None elsewhere
    tables: tuple[tuple[str, int], ...]  # largest first


def connect_readonly(url: str) -> Connection:
    return make_readonly(db.connect(url))


def make_readonly(conn: Connection) -> Connection:
    # Session-wide, so even single statements outside a transaction are read-only. A guard
    # against accidents; a SELECT-only role is the stronger version if this is ever hosted.
    conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
    return conn


_RUN_COLUMNS = """r.run_id, r.status, r.started_at, r.finished_at, r.api_calls, r.reported_total,
    r.fetched_total, r.unique_events, r.undated_events,
    (SELECT count(*) FROM event_changes c WHERE c.run_id = r.run_id),
    r.checks_failed, r.pruned_raw, r.error"""


def recent_runs(conn: Connection, limit: int = 24) -> list[RunRow]:
    rows = conn.execute(
        f"SELECT {_RUN_COLUMNS} FROM ingest_runs r ORDER BY r.run_id DESC LIMIT %s",
        (limit,),
    ).fetchall()
    return [RunRow(*r) for r in rows]


def latest_run(conn: Connection) -> RunRow | None:
    runs = recent_runs(conn, limit=1)
    return runs[0] if runs else None


def last_success_at(conn: Connection) -> datetime | None:
    row = conn.execute("SELECT max(finished_at) FROM ingest_runs WHERE status = 'succeeded'"
                       ).fetchone()
    return row[0] if row else None


def completeness(conn: Connection) -> Completeness | None:
    row = conn.execute(
        """SELECT r.run_id, r.finished_at, c.expected, c.observed, coalesce(r.undated_events, 0)
           FROM ingest_runs r
           JOIN check_results c ON c.run_id = r.run_id AND c.check_name = 'unique_vs_total'
           WHERE r.status = 'succeeded' AND c.expected IS NOT NULL
           ORDER BY r.run_id DESC LIMIT 1""").fetchone()
    if row is None:
        return None
    return Completeness(row[0], row[1], int(row[2]), int(row[3]), int(row[4]))


def latest_checks(conn: Connection) -> tuple[int, list[CheckRow]] | None:
    """The latest run's checks, problems first: failed errors, then warnings, then the rest."""
    run = conn.execute("SELECT max(run_id) FROM check_results").fetchone()
    if run is None or run[0] is None:
        return None
    rows = conn.execute(
        """SELECT check_name, passed, severity, detail FROM check_results WHERE run_id = %s
           ORDER BY passed, severity, check_name""", (run[0],)).fetchall()
    return int(run[0]), [CheckRow(*r) for r in rows]


def recent_changes(conn: Connection, since: datetime, limit: int = 200) -> list[ChangeRow]:
    rows = conn.execute(
        """SELECT c.detected_at, e.name, v.name, e.local_date, c.field,
                  CASE WHEN c.field = 'venue' THEN coalesce(vo.name, c.old_value)
                       ELSE c.old_value END,
                  CASE WHEN c.field = 'venue' THEN coalesce(vn.name, c.new_value)
                       ELSE c.new_value END
           FROM event_changes c
           JOIN events e USING (event_id)
           LEFT JOIN venues v ON v.venue_id = e.venue_id
           LEFT JOIN venues vo ON c.field = 'venue' AND vo.source = e.source
                               AND vo.source_id = c.old_value
           LEFT JOIN venues vn ON c.field = 'venue' AND vn.source = e.source
                               AND vn.source_id = c.new_value
           WHERE c.detected_at >= %s
           ORDER BY c.detected_at DESC, e.name, c.field
           LIMIT %s""", (since, limit)).fetchall()
    return [ChangeRow(*r) for r in rows]


def new_shows(conn: Connection, since: datetime, limit: int = 200) -> list[NewShow]:
    """Shows first seen since `since`. The first-ever run is the baseline, not news."""
    rows = conn.execute(
        """SELECT r.started_at, e.name, v.name, e.local_date, e.local_time
           FROM events e
           JOIN ingest_runs r ON r.run_id = e.first_seen_run
           LEFT JOIN venues v ON v.venue_id = e.venue_id
           WHERE r.started_at >= %s AND e.first_seen_run > (SELECT min(run_id) FROM ingest_runs)
           ORDER BY r.started_at DESC, e.local_date NULLS LAST, e.name
           LIMIT %s""", (since, limit)).fetchall()
    return [NewShow(*r) for r in rows]


def onsales_between(conn: Connection, start: datetime, end: datetime) -> list[Onsale]:
    rows = conn.execute(
        f"""SELECT e.public_sale_start, e.name, v.name, e.local_date
            FROM events e LEFT JOIN venues v ON v.venue_id = e.venue_id
            WHERE e.public_sale_start >= %s AND e.public_sale_start < %s AND {_LISTED}
            ORDER BY e.public_sale_start, e.name""",
        (start, end)).fetchall()
    return [Onsale(*r) for r in rows]


def upcoming_events(
    conn: Connection, start: date, end: date, venues: Sequence[str] = (),
    statuses: Sequence[str] = (), limit: int = 500,
) -> tuple[list[EventRow], int]:
    """Listed events dated start..end (inclusive), optionally filtered; capped, plus the total."""
    rows = conn.execute(
        f"""SELECT e.name, v.name, v.city, e.local_date, e.local_time, e.status,
                   e.public_sale_start, count(*) OVER ()
            FROM events e LEFT JOIN venues v ON v.venue_id = e.venue_id
            WHERE e.local_date BETWEEN %(start)s AND %(end)s AND {_LISTED}
              AND (cardinality(%(venues)s::text[]) = 0 OR v.name = ANY(%(venues)s))
              AND (cardinality(%(statuses)s::text[]) = 0 OR e.status = ANY(%(statuses)s))
            ORDER BY e.local_date, e.local_time NULLS LAST, e.name
            LIMIT %(limit)s""",
        {"start": start, "end": end, "venues": list(venues), "statuses": list(statuses),
         "limit": limit}).fetchall()
    total = int(rows[0][7]) if rows else 0
    return [EventRow(*r[:7]) for r in rows], total


def filter_options(conn: Connection, start: date, end: date) -> tuple[list[str], list[str]]:
    where = f"e.local_date BETWEEN %s AND %s AND {_LISTED}"
    venues = conn.execute(
        f"""SELECT DISTINCT v.name FROM events e JOIN venues v ON v.venue_id = e.venue_id
            WHERE {where} ORDER BY 1""", (start, end)).fetchall()
    statuses = conn.execute(
        f"""SELECT DISTINCT e.status FROM events e WHERE {where} AND e.status IS NOT NULL
            ORDER BY 1""", (start, end)).fetchall()
    return [r[0] for r in venues], [r[0] for r in statuses]


def events_per_week(conn: Connection, start: date, weeks: int = 13) -> list[tuple[date, int]]:
    """Listed events per LA week (Monday first), every week present even when empty."""
    rows = conn.execute(
        f"""SELECT w::date, count(e.event_id)
            FROM generate_series(date_trunc('week', %(start)s::date),
                                 date_trunc('week', %(start)s::date)
                                   + (%(weeks)s - 1) * interval '1 week',
                                 interval '1 week') AS w
            LEFT JOIN events e ON date_trunc('week', e.local_date) = w
                               AND e.local_date >= %(start)s AND {_LISTED}
            GROUP BY w ORDER BY w""",
        {"start": start, "weeks": weeks}).fetchall()
    return [(r[0], int(r[1])) for r in rows]


_UNIT_BYTES = {"kB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}


def storage(conn: Connection) -> Storage:
    size = conn.execute("SELECT pg_database_size(current_database())").fetchone()
    limit = conn.execute("SELECT setting, unit FROM pg_settings"
                         " WHERE name = 'neon.max_cluster_size'").fetchone()
    tables = conn.execute(
        """SELECT c.relname, pg_total_relation_size(c.oid)
           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = current_schema() AND c.relkind = 'r'
           ORDER BY 2 DESC, 1""").fetchall()
    limit_bytes = (int(limit[0]) * _UNIT_BYTES.get(limit[1] or "MB", 1)
                   if limit and limit[0] and int(limit[0]) > 0 else None)
    return Storage(int(size[0]) if size else 0, limit_bytes,
                   tuple((str(r[0]), int(r[1])) for r in tables))
