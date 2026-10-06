"""Dashboard queries against a real Postgres (MARQUEE_TEST_DATABASE_URL), on data loaded by real
ingests through the fake API. Every dashboard connection is read-only."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest
from conftest import Schema
from fake_discovery import FakeDiscovery, FakeEvent, client_for, spread, undated

from marquee import queries
from marquee.db import Connection, migrate
from marquee.ingest import ingest
from marquee.windows import plan_range

NOW = datetime(2026, 10, 6, 15, 22, 49, tzinfo=UTC)
WHOLE = plan_range(NOW)
TODAY = date(2026, 10, 6)
LA = ZoneInfo("America/Los_Angeles")


@pytest.fixture
def migrated(schema: Schema) -> Schema:
    with schema.connect() as conn:
        migrate(conn)
    return schema


def readonly(schema: Schema) -> Connection:
    return queries.make_readonly(schema.connect())


@pytest.fixture
def loaded(migrated: Schema) -> Schema:
    """Two runs. Between them: one show cancelled, one moved to another date, one moved to
    another venue, one new show, one show that vanished from the API."""
    events = spread(60, WHOLE.start, WHOLE.end) + undated(2)
    events.append(FakeEvent("evtGONE", datetime(2026, 10, 20, 3, tzinfo=UTC)))
    api = FakeDiscovery(events)
    with migrated.connect() as conn:
        ingest(conn, client_for(api), now=NOW)
        api.update("evt00001", status="cancelled")
        api.update("evt00002", starts_at=datetime(2026, 12, 3, 4, tzinfo=UTC))
        api.update("evt00003", venue="KovFAKE002")
        api.events = [e for e in api.events if e.id != "evtGONE"]
        api.events.append(FakeEvent("evtNEW", datetime(2026, 10, 9, 3, tzinfo=UTC),
                                    onsale="2026-10-08T17:00:00Z"))
        ingest(conn, client_for(api), now=NOW)
    return migrated


# --- Read-only by design --------------------------------------------------------------------------

@pytest.mark.parametrize("statement", [
    "INSERT INTO ingest_runs (source) VALUES ('ticketmaster')",
    "UPDATE events SET status = 'bogus'",
    "DELETE FROM events",
    "TRUNCATE events CASCADE",
    "CREATE TABLE sneaky (i int)",
])
def test_a_dashboard_connection_refuses_every_write(loaded: Schema, statement: str) -> None:
    with readonly(loaded) as conn, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute(statement)  # type: ignore[arg-type]


def test_a_dashboard_connection_reads_and_has_a_statement_timeout(loaded: Schema) -> None:
    with readonly(loaded) as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone() == (64,)
        assert conn.execute("SHOW statement_timeout").fetchone() == ("5s",)


# --- An empty database: every query has an answer, none raises -----------------------------------

def test_every_query_copes_with_an_empty_database(migrated: Schema) -> None:
    since = datetime.now(UTC) - timedelta(days=1)
    with readonly(migrated) as conn:
        assert queries.latest_run(conn) is None
        assert queries.last_success_at(conn) is None
        assert queries.recent_runs(conn) == []
        assert queries.completeness(conn) is None
        assert queries.latest_checks(conn) is None
        assert queries.recent_changes(conn, since) == []
        assert queries.new_shows(conn, since) == []
        assert queries.onsales_between(conn, NOW, NOW + timedelta(days=7)) == []
        assert queries.upcoming_events(conn, TODAY, TODAY + timedelta(days=14)) == ([], 0)
        assert queries.filter_options(conn, TODAY, TODAY + timedelta(days=90)) == ([], [])
        weeks = queries.events_per_week(conn, TODAY, weeks=13)
        assert len(weeks) == 13 and all(n == 0 for _, n in weeks)
        assert queries.storage(conn).database_bytes > 0


# --- With data ------------------------------------------------------------------------------------

def test_runs_newest_first_with_their_counts(loaded: Schema) -> None:
    with readonly(loaded) as conn:
        runs = queries.recent_runs(conn)
        latest = queries.latest_run(conn)
        success = queries.last_success_at(conn)
    assert [r.run_id for r in runs] == [2, 1]
    assert latest is not None and latest.run_id == 2 and latest.status == "succeeded"
    assert runs[0].changes == 4 and runs[1].changes == 0  # status, date, time, venue
    assert (runs[0].api_calls, runs[0].unique_events, runs[0].undated_events) == (16, 63, 2)
    assert success is not None and success == runs[0].finished_at


def test_completeness_comes_from_the_latest_succeeded_run(loaded: Schema) -> None:
    with readonly(loaded) as conn:
        c = queries.completeness(conn)
    assert c is not None and (c.run_id, c.reported, c.received, c.undated) == (2, 61, 61, 2)


def test_checks_put_problems_first(migrated: Schema) -> None:
    events = spread(20, WHOLE.start, WHOLE.end)
    events[0] = FakeEvent(events[0].id, events[0].starts_at, onsale="2027-06-01T17:00:00Z")
    with migrated.connect() as conn:
        ingest(conn, client_for(FakeDiscovery(events)), now=NOW)
    with readonly(migrated) as conn:
        found = queries.latest_checks(conn)
    assert found is not None
    run_id, rows = found
    assert run_id == 1 and len(rows) == 7
    assert (rows[0].name, rows[0].passed, rows[0].severity) == \
        ("implausible_onsales", False, "warning")
    assert all(r.passed for r in rows[1:])


def moved_from() -> tuple[str, str]:
    """evt00002's original local date and time, before the move to Dec 2, 8:00 PM."""
    local = spread(60, WHOLE.start, WHOLE.end)[2].starts_at.astimezone(LA)  # type: ignore[union-attr]
    return local.date().isoformat(), local.strftime("%H:%M:%S")


def test_changes_read_like_a_traders_note_newest_first(loaded: Schema) -> None:
    with readonly(loaded) as conn:
        rows = queries.recent_changes(conn, datetime.now(UTC) - timedelta(days=1))
    old_date, old_time = moved_from()
    assert {(r.show, r.venue, r.field, r.old, r.new) for r in rows} == {
        ("Synthetic evt00001", "Venue KovFAKE001", "status", "onsale", "cancelled"),
        ("Synthetic evt00002", "Venue KovFAKE001", "local_date", old_date, "2026-12-02"),
        ("Synthetic evt00002", "Venue KovFAKE001", "local_time", old_time, "20:00:00"),
        # Venue changes are stored as Ticketmaster ids and shown as names.
        ("Synthetic evt00003", "Venue KovFAKE002", "venue", "Venue KovFAKE001",
         "Venue KovFAKE002"),
    }
    assert [r.detected_at for r in rows] == sorted((r.detected_at for r in rows), reverse=True)
    moved = next(r for r in rows if r.field == "local_date")
    assert moved.event_date == date(2026, 12, 2)  # the event's date now


def test_new_shows_ignore_the_first_ever_run(loaded: Schema) -> None:
    with readonly(loaded) as conn:
        shows = queries.new_shows(conn, datetime.now(UTC) - timedelta(days=1))
    assert [(s.show, s.venue, s.event_date) for s in shows] == \
        [("Synthetic evtNEW", "Venue KovFAKE001", date(2026, 10, 8))]


def test_onsales_are_bounded_to_the_window(loaded: Schema) -> None:
    with readonly(loaded) as conn:
        week = queries.onsales_between(conn, NOW, NOW + timedelta(days=7))
        later = queries.onsales_between(conn, NOW + timedelta(days=7), NOW + timedelta(days=30))
    assert [(o.show, o.onsale_at) for o in week] == \
        [("Synthetic evtNEW", datetime(2026, 10, 8, 17, tzinfo=UTC))]
    assert later == []


def test_upcoming_events_filter_and_count(loaded: Schema) -> None:
    end = TODAY + timedelta(days=14)
    with readonly(loaded) as conn:
        rows, total = queries.upcoming_events(conn, TODAY, end)
        cancelled, n_cancelled = queries.upcoming_events(conn, TODAY, TODAY + timedelta(days=90),
                                                         statuses=["cancelled"])
        none, n_none = queries.upcoming_events(conn, TODAY, end, venues=["No Such Venue"])
        capped, n_capped = queries.upcoming_events(conn, TODAY, end, limit=3)
        venues, statuses = queries.filter_options(conn, TODAY, TODAY + timedelta(days=90))
    assert total == len(rows) > 0
    assert all(TODAY <= r.event_date <= end for r in rows if r.event_date)
    assert "Synthetic evtGONE" not in {r.show for r in rows}  # vanished: no longer listed
    assert [r.show for r in cancelled] == ["Synthetic evt00001"] and n_cancelled == 1
    assert (none, n_none) == ([], 0)
    assert len(capped) == 3 and n_capped == total
    assert venues == ["Venue KovFAKE001", "Venue KovFAKE002"]
    assert statuses == ["cancelled", "onsale"]


def test_weekly_counts_cover_every_listed_dated_event(loaded: Schema) -> None:
    with readonly(loaded) as conn:
        weeks = queries.events_per_week(conn, TODAY, weeks=14)
        _, listed = queries.upcoming_events(conn, TODAY, TODAY + timedelta(days=97))
    assert [w for w, _ in weeks][0] == date(2026, 10, 5)  # weeks start on Monday
    assert sum(n for _, n in weeks) == listed == 61


def test_storage_lists_the_tables_and_the_database_size(loaded: Schema) -> None:
    with readonly(loaded) as conn:
        s = queries.storage(conn)
    names = [name for name, _ in s.tables]
    assert {"events", "raw_responses", "venues"} <= set(names)
    assert s.database_bytes >= sum(size for _, size in s.tables)
    assert s.limit_bytes is None  # only Neon has neon.max_cluster_size
