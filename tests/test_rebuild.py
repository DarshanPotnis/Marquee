"""Rebuild from raw: onto the live tables (repair), and into a throwaway schema (--verify).

Uses MARQUEE_TEST_DATABASE_URL and skips cleanly without it.
"""

import logging
from datetime import UTC, datetime

import pytest
from conftest import Schema
from fake_discovery import FakeDiscovery, FakeEvent, client_for, spread, undated

import marquee.__main__ as cli
import marquee.rebuild as rebuild_module
from marquee import db
from marquee.config import load_settings
from marquee.db import INGEST_LOCK, LockHeld, migrate
from marquee.ingest import ingest
from marquee.rebuild import RebuildError, rebuild, verify
from marquee.windows import plan_range

NOW = datetime(2026, 10, 6, 15, 22, 49, tzinfo=UTC)
WHOLE = plan_range(NOW)
LIVE = ("venues", "attractions", "events", "event_attractions", "event_changes", "ingest_runs",
        "raw_responses")


@pytest.fixture
def migrated(schema: Schema) -> Schema:
    with schema.connect() as conn:
        migrate(conn)
    return schema


def ingest_twice(schema: Schema, api: FakeDiscovery) -> None:
    for _ in range(2):
        with schema.connect() as conn:
            ingest(conn, client_for(api), now=NOW)


def snapshot(schema: Schema) -> dict[str, object]:
    """Every live row (updated_at included) and the id sequences: anything verify might touch."""
    with schema.connect() as conn:
        rows: dict[str, object] = {
            t: conn.execute(f"SELECT md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), ''))"
                            f" FROM {t} x").fetchone()
            for t in LIVE
        }
        rows["sequences"] = conn.execute(
            "SELECT string_agg(sequencename || '=' || coalesce(last_value, 0), ',' ORDER BY 1)"
            " FROM pg_sequences WHERE schemaname = current_schema()").fetchone()
        rows["schemas"] = conn.execute(
            "SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'marquee_verify_%'").fetchone()
        return rows


def diffs(v: object) -> dict[str, tuple[int, int]]:
    return {d.table: (d.only_live, d.only_rebuilt) for d in v.diffs}  # type: ignore[attr-defined]


def pruned(v: object) -> dict[str, int]:
    return {d.table: d.pruned for d in v.diffs}  # type: ignore[attr-defined]


# --- --verify: rebuild into a throwaway schema and compare ----------------------------------------

def test_verify_finds_no_differences_after_two_ingests_and_changes_nothing(
    migrated: Schema,
) -> None:
    ingest_twice(migrated, FakeDiscovery(spread(300, WHOLE.start, WHOLE.end) + undated(3)))
    before = snapshot(migrated)
    with migrated.connect() as conn:
        v = verify(conn)
    assert v.ok
    assert v.raw_pages == 30
    assert diffs(v) == {"venues": (0, 0), "attractions": (0, 0), "events": (0, 0),
                        "event_attractions": (0, 0)}
    assert snapshot(migrated) == before  # no live row, sequence or leftover schema changed


def test_verify_reports_a_tampered_live_row_and_leaves_it_alone(migrated: Schema) -> None:
    ingest_twice(migrated, FakeDiscovery(spread(50, WHOLE.start, WHOLE.end)))
    with migrated.connect() as conn:
        conn.execute("UPDATE events SET status = 'bogus' WHERE source_id = 'evt00003'")
        v = verify(conn)
        assert conn.execute("SELECT status FROM events WHERE source_id = 'evt00003'"
                            ).fetchone() == ("bogus",)
    assert not v.ok
    assert diffs(v)["events"] == (1, 1)
    assert diffs(v)["venues"] == diffs(v)["attractions"] == diffs(v)["event_attractions"] == (0, 0)
    assert "evt00003" in next(d for d in v.diffs if d.table == "events").samples


# --- Plain rebuild: replay onto the live tables (the repair tool) --------------------------------

def test_rebuild_repairs_the_live_tables_after_a_transform_fix(migrated: Schema) -> None:
    ingest_twice(migrated, FakeDiscovery(spread(50, WHOLE.start, WHOLE.end)))
    with migrated.connect() as conn:
        conn.execute("UPDATE events SET status = 'bogus' WHERE source_id = 'evt00003'")
        r = rebuild(conn)
        assert r.raw_pages == 30
        assert conn.execute("SELECT status FROM events WHERE source_id = 'evt00003'"
                            ).fetchone() == ("onsale",)
        assert verify(conn).ok


def test_rebuild_never_touches_event_changes(migrated: Schema) -> None:
    api = FakeDiscovery(spread(50, WHOLE.start, WHOLE.end))
    ingest_twice(migrated, api)
    api.update("evt00007", status="cancelled")
    with migrated.connect() as conn:
        ingest(conn, client_for(api), now=NOW)
        history = conn.execute("SELECT * FROM event_changes ORDER BY 1, 2, 4").fetchall()
        assert len(history) == 1
        rebuild(conn)
        assert conn.execute("SELECT * FROM event_changes ORDER BY 1, 2, 4").fetchall() == history


def test_after_pruning_rows_without_raw_are_left_alone_and_verify_says_so(
    migrated: Schema,
) -> None:
    # What rebuild can't reproduce once raw is pruned (docs/decisions/001).
    gone = FakeEvent("evtGONE", datetime(2026, 11, 20, 4, tzinfo=UTC))
    kept = FakeEvent("evtKEPT", datetime(2026, 11, 21, 4, tzinfo=UTC))
    api = FakeDiscovery([gone, kept])
    with migrated.connect() as conn:
        first = ingest(conn, client_for(api), now=NOW)
        api.events = [kept]
        ingest(conn, client_for(api), now=NOW)
        assert first is not None
        conn.execute("DELETE FROM raw_responses WHERE run_id = %s", (first.run_id,))  # pruned
        rebuild(conn)
        rows = dict(conn.execute("SELECT source_id, first_seen_run FROM events").fetchall())
        assert rows == {"evtGONE": first.run_id, "evtKEPT": first.run_id}  # left alone, not moved
        v = verify(conn)
    # Only what retained raw can reproduce is compared. evtGONE (last seen in the pruned run) and
    # its attraction, and evtKEPT's first_seen_run, are reported as not reproducible, not failed.
    assert v.ok and diffs(v)["events"] == (0, 0)
    assert pruned(v) == {"venues": 0, "attractions": 1, "events": 1, "event_attractions": 1}
    assert v.first_seen_pruned == 1


def test_after_pruning_verify_still_catches_a_real_difference(migrated: Schema) -> None:
    gone = FakeEvent("evtGONE", datetime(2026, 11, 20, 4, tzinfo=UTC))
    kept = FakeEvent("evtKEPT", datetime(2026, 11, 21, 4, tzinfo=UTC))
    api = FakeDiscovery([gone, kept])
    with migrated.connect() as conn:
        first = ingest(conn, client_for(api), now=NOW)
        api.events = [kept]
        ingest(conn, client_for(api), now=NOW)
        assert first is not None
        conn.execute("DELETE FROM raw_responses WHERE run_id = %s", (first.run_id,))
        conn.execute("UPDATE events SET status = 'bogus' WHERE source_id = 'evtKEPT'")
        v = verify(conn)
    assert not v.ok and diffs(v)["events"] == (1, 1)


def test_a_corrupted_raw_body_stops_the_rebuild_and_changes_nothing(migrated: Schema) -> None:
    ingest_twice(migrated, FakeDiscovery(spread(50, WHOLE.start, WHOLE.end)))
    with migrated.connect() as conn:
        conn.execute("UPDATE events SET status = 'bogus' WHERE source_id = 'evt00003'")
        conn.execute("UPDATE raw_responses SET body_sha256 = repeat('0', 64)"
                     " WHERE raw_id = (SELECT max(raw_id) FROM raw_responses)")
        with pytest.raises(RebuildError, match="SHA-256"):
            rebuild(conn)
        assert conn.execute("SELECT status FROM events WHERE source_id = 'evt00003'"
                            ).fetchone() == ("bogus",)  # rolled back: nothing half-rebuilt


# --- The command: zero API calls ------------------------------------------------------------------

def use(monkeypatch: pytest.MonkeyPatch, schema: Schema) -> None:
    settings = load_settings({"MARQUEE_DATABASE_URL": schema.url})  # no TM_API_KEY needed

    def no_api(_: object) -> None:
        raise AssertionError("rebuild must not create an API client")

    monkeypatch.setattr(cli, "settings_from_environment", lambda: settings)
    monkeypatch.setattr(cli, "make_client", no_api)
    monkeypatch.setattr(db, "connect", lambda url: schema.connect())


def test_rebuild_verify_command_reports_and_exits_by_result(
    migrated: Schema, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ingest_twice(migrated, FakeDiscovery(spread(50, WHOLE.start, WHOLE.end)))
    use(monkeypatch, migrated)
    with caplog.at_level(logging.INFO):
        assert cli.main(["rebuild", "--verify"]) == 0
        assert "differences: none" in caplog.text
        with migrated.connect() as conn:
            conn.execute("UPDATE events SET status = 'bogus' WHERE source_id = 'evt00003'")
        assert cli.main(["rebuild", "--verify"]) == 1
        assert "events: 1 only live, 1 only rebuilt, 0 not reproducible (pruned)" in caplog.text


def test_rebuild_command_replays_onto_the_live_tables(
    migrated: Schema, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ingest_twice(migrated, FakeDiscovery(spread(50, WHOLE.start, WHOLE.end)))
    use(monkeypatch, migrated)
    with caplog.at_level(logging.INFO):
        assert cli.main(["rebuild"]) == 0
    assert "replayed 30 raw pages from 2 runs onto the live tables" in caplog.text


# --- rebuild and ingest never overlap -------------------------------------------------------------

def test_rebuild_and_verify_refuse_while_an_ingest_holds_the_lock(
    migrated: Schema, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ingest_twice(migrated, FakeDiscovery(spread(20, WHOLE.start, WHOLE.end)))
    with migrated.connect() as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (INGEST_LOCK.key,))
        with migrated.connect() as conn:
            with pytest.raises(LockHeld, match=r"marquee\.ingest"):
                rebuild(conn)
            with pytest.raises(LockHeld, match=r"marquee\.ingest"):
                verify(conn)
        use(monkeypatch, migrated)
        with caplog.at_level(logging.INFO):
            assert cli.main(["rebuild"]) == 1
        assert "an ingest is running" in caplog.text


def test_ingest_stands_aside_while_a_rebuild_holds_the_lock(
    migrated: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeDiscovery(spread(20, WHOLE.start, WHOLE.end))
    ingest_twice(migrated, api)
    attempts: list[object] = []
    replay = rebuild_module._replay

    def replay_while_an_ingest_tries(conn: object, schema: str) -> object:
        with migrated.connect() as other:
            attempts.append(ingest(other, client_for(api), now=NOW))
        return replay(conn, schema)  # type: ignore[arg-type]

    monkeypatch.setattr(rebuild_module, "_replay", replay_while_an_ingest_tries)
    with migrated.connect() as conn:
        rebuild(conn)
    assert attempts == [None]  # the ingest saw the lock and did nothing
