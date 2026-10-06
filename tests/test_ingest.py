"""One ingest run end to end: fake API in, a throwaway Postgres schema out.

Uses MARQUEE_TEST_DATABASE_URL and skips cleanly without it.
"""

import gzip
import hashlib
import logging
from datetime import UTC, datetime

import httpx
import pytest
from conftest import Schema
from fake_discovery import FakeDiscovery, FakeEvent, client_for, page_of, spread, undated

import marquee.__main__ as cli
from marquee import db
from marquee.config import load_settings
from marquee.db import INGEST_LOCK, migrate
from marquee.db import connect as direct_connect
from marquee.ingest import RunSummary, ingest
from marquee.load import load_rows
from marquee.tm_client import QUOTA_VIOLATION
from marquee.transform import transform_page
from marquee.windows import plan_range

NOW = datetime(2026, 10, 6, 15, 22, 49, tzinfo=UTC)
WHOLE = plan_range(NOW)
CLEAN = ("venues", "attractions", "events", "event_attractions", "event_changes")


@pytest.fixture
def migrated(schema: Schema) -> Schema:
    with schema.connect() as conn:
        migrate(conn)
    return schema


def run(schema: Schema, api: FakeDiscovery, **kwargs: int) -> RunSummary | None:
    with schema.connect() as conn:
        return ingest(conn, client_for(api), now=NOW, **kwargs)


def counts(schema: Schema) -> dict[str, int]:
    with schema.connect() as conn:
        return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # type: ignore[index]
                for t in (*CLEAN, "raw_responses", "ingest_runs")}


def query(schema: Schema, sql: str, *params: object) -> list[tuple[object, ...]]:
    with schema.connect() as conn:
        return conn.execute(sql, params).fetchall()  # type: ignore[arg-type]


# --- A normal run ------------------------------------------------------------------------------

def test_a_first_ingest_loads_the_windows_and_the_undated_events(migrated: Schema) -> None:
    api = FakeDiscovery(spread(300, WHOLE.start, WHOLE.end) + undated(3))
    s = run(migrated, api)
    assert s is not None and s.status == "succeeded"
    assert (s.windows, s.api_calls, s.reported_total, s.fetched_total) == (13, 15, 300, 300)
    assert (s.unique_events, s.undated_events, s.probe_calls, s.probe_events) == (303, 3, 0, 0)
    assert (s.changes, s.onsale_nulled) == (0, {})
    c = counts(migrated)
    assert (c["events"], c["venues"], c["attractions"], c["event_attractions"]) == \
        (303, 1, 303, 303)
    assert c["raw_responses"] == 15 == len(api.requests)
    assert c["event_changes"] == 0
    assert query(migrated, "SELECT status, finished_at IS NOT NULL, api_calls, undated_events,"
                           " onsale_nulled FROM ingest_runs") == \
        [("succeeded", True, 15, 3, {})]
    assert query(migrated, "SELECT count(*) FROM events WHERE local_date IS NULL"
                           " AND status = 'postponed'") == [(3,)]


def test_a_second_identical_run_adds_no_rows_and_no_changes(migrated: Schema) -> None:
    api = FakeDiscovery(spread(300, WHOLE.start, WHOLE.end) + undated(3))
    first = run(migrated, api)
    before = counts(migrated)
    stamps = query(migrated, "SELECT source_id, updated_at FROM events ORDER BY source_id")
    second = run(migrated, api)
    after = counts(migrated)
    assert first is not None and second is not None
    assert second.changes == 0
    assert {t: after[t] for t in CLEAN} == {t: before[t] for t in CLEAN}
    assert after["raw_responses"] == before["raw_responses"] + 15
    assert after["ingest_runs"] == before["ingest_runs"] + 1
    # Rows are refreshed, not rewritten: nothing changed, so updated_at didn't move.
    assert query(migrated, "SELECT source_id, updated_at FROM events ORDER BY source_id") == stamps
    assert query(migrated, "SELECT DISTINCT first_seen_run, last_seen_run FROM events") == \
        [(first.run_id, second.run_id)]


def test_raw_is_kept_exactly_as_received(migrated: Schema) -> None:
    api = FakeDiscovery(spread(450, WHOLE.start, WHOLE.end))
    run(migrated, api)
    rows = query(migrated, "SELECT params, body_gzip, body_sha256, body_bytes"
                           " FROM raw_responses ORDER BY raw_id")
    assert len(rows) == len(api.sent)
    for (params, packed, sha, size), sent in zip(rows, api.sent, strict=True):
        assert gzip.decompress(packed) == sent  # type: ignore[arg-type]
        assert (sha, size) == (hashlib.sha256(sent).hexdigest(), len(sent))
        assert "apikey" not in {k.lower() for k in params}  # type: ignore[union-attr]


def test_probe_pages_are_saved_and_counted_apart(migrated: Schema) -> None:
    api = FakeDiscovery(spread(300, WHOLE.start, WHOLE.end))  # about 23 a week, 3 a day
    s = run(migrated, api, split_threshold=20)
    assert s is not None and s.probe_calls > 0 and s.probe_events > 0
    assert counts(migrated)["raw_responses"] == s.api_calls == len(api.requests)
    assert (s.unique_events, counts(migrated)["events"]) == (300, 300)


def test_implausible_onsales_are_nulled_and_counted_on_the_run(migrated: Schema) -> None:
    events = spread(10, WHOLE.start, WHOLE.end)
    events[0:3] = [FakeEvent(e.id, e.starts_at, onsale="1900-01-01T18:00:00Z") for e in events[0:3]]
    events[3] = FakeEvent(events[3].id, events[3].starts_at, onsale="2027-06-01T17:00:00Z")
    s = run(migrated, FakeDiscovery(events))
    assert s is not None and s.onsale_nulled == {"placeholder_1900": 3, "after_event": 1}
    assert query(migrated, "SELECT onsale_nulled FROM ingest_runs") == \
        [({"placeholder_1900": 3, "after_event": 1},)]
    assert query(migrated, "SELECT count(*) FROM events WHERE public_sale_start IS NULL") == [(4,)]


# --- Changes -----------------------------------------------------------------------------------

def changes(schema: Schema, run_id: int) -> set[tuple[object, ...]]:
    return set(query(schema, "SELECT e.source_id, c.field, c.old_value, c.new_value"
                             " FROM event_changes c JOIN events e USING (event_id)"
                             " WHERE c.run_id = %s", run_id))


def test_a_change_between_runs_is_recorded(migrated: Schema) -> None:
    api = FakeDiscovery(spread(50, WHOLE.start, WHOLE.end))
    run(migrated, api)
    api.update("evt00007", status="cancelled", venue="KovFAKE002")
    s = run(migrated, api)
    assert s is not None and s.changes == 2
    assert changes(migrated, s.run_id) == {("evt00007", "status", "onsale", "cancelled"),
                                           ("evt00007", "venue", "KovFAKE001", "KovFAKE002")}


def test_an_undated_event_getting_a_date_is_a_change(migrated: Schema) -> None:
    api = FakeDiscovery(spread(20, WHOLE.start, WHOLE.end) + undated(1))
    run(migrated, api)
    api.update("tba000", starts_at=datetime(2026, 12, 5, 4, 0, tzinfo=UTC), status="rescheduled")
    s = run(migrated, api)
    assert s is not None
    assert changes(migrated, s.run_id) == {("tba000", "status", "postponed", "rescheduled"),
                                           ("tba000", "local_date", None, "2026-12-04"),
                                           ("tba000", "local_time", None, "20:00:00")}


def test_a_repeat_change_within_one_run_updates_that_runs_row(migrated: Schema) -> None:
    # Seen on a probe page, then again in its child window seconds later, changed in between.
    show = FakeEvent("evtREPEAT", datetime(2026, 11, 20, 4, tzinfo=UTC))
    run(migrated, FakeDiscovery([show]))
    with migrated.connect() as conn:
        run_id = conn.execute("INSERT INTO ingest_runs (source) VALUES ('ticketmaster')"
                              " RETURNING run_id").fetchone()[0]  # type: ignore[index]
        for status in ("postponed", "cancelled"):
            with conn.transaction():
                load_rows(conn, run_id, transform_page(
                    page_of(FakeEvent(show.id, show.starts_at, status=status)), run_at=NOW))
    assert changes(migrated, run_id) == {("evtREPEAT", "status", "onsale", "cancelled")}


def test_a_repeat_change_back_to_the_start_leaves_no_row(migrated: Schema) -> None:
    show = FakeEvent("evtREPEAT", datetime(2026, 11, 20, 4, tzinfo=UTC))
    run(migrated, FakeDiscovery([show]))
    with migrated.connect() as conn:
        run_id = conn.execute("INSERT INTO ingest_runs (source) VALUES ('ticketmaster')"
                              " RETURNING run_id").fetchone()[0]  # type: ignore[index]
        for status in ("postponed", "onsale"):
            with conn.transaction():
                load_rows(conn, run_id, transform_page(
                    page_of(FakeEvent(show.id, show.starts_at, status=status)), run_at=NOW))
    assert changes(migrated, run_id) == set()


# --- When things go wrong ----------------------------------------------------------------------

def test_a_held_lock_means_the_run_exits_quietly(migrated: Schema) -> None:
    api = FakeDiscovery(spread(10, WHOLE.start, WHOLE.end))
    with direct_connect(migrated.url) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (INGEST_LOCK.key,))
        assert run(migrated, api) is None
    assert counts(migrated)["ingest_runs"] == 0
    assert api.requests == []


def quota_429() -> httpx.Response:
    return httpx.Response(429, json={"fault": {
        "faultstring": "Rate limit quota violation. Quota limit exceeded.",
        "detail": {"errorcode": QUOTA_VIOLATION}}})


def test_a_quota_stop_mid_run_ends_partial_and_keeps_the_pages_so_far(migrated: Schema) -> None:
    api = FakeDiscovery(spread(300, WHOLE.start, WHOLE.end))
    api.failures[5] = quota_429
    s = run(migrated, api)
    assert s is not None and s.status == "partial"
    assert "quota" in (s.error or "")
    assert counts(migrated)["raw_responses"] == 4
    assert query(migrated, "SELECT status FROM ingest_runs") == [("partial",)]
    assert counts(migrated)["events"] > 0


def test_a_crash_mid_run_keeps_earlier_pages_and_a_rerun_is_clean(migrated: Schema) -> None:
    api = FakeDiscovery(spread(300, WHOLE.start, WHOLE.end))
    for n in range(5, 10):  # five 500s in a row: the client gives up
        api.failures[n] = lambda: httpx.Response(500)
    s = run(migrated, api)
    assert s is not None and s.status == "failed"
    assert counts(migrated)["raw_responses"] == 4
    api.failures.clear()
    again = run(migrated, api)
    assert again is not None and again.status == "succeeded"
    assert counts(migrated)["events"] == 300
    assert query(migrated, "SELECT count(DISTINCT source_id) = count(*) FROM events") == [(True,)]


# --- The command ---------------------------------------------------------------------------------

def use(monkeypatch: pytest.MonkeyPatch, schema: Schema, api: FakeDiscovery) -> None:
    settings = load_settings({"MARQUEE_DATABASE_URL": schema.url, "TM_API_KEY": "k"})
    monkeypatch.setattr(cli, "settings_from_environment", lambda: settings)
    monkeypatch.setattr(cli, "make_client", lambda s: client_for(api))
    monkeypatch.setattr(db, "connect", lambda url: schema.connect())
    monkeypatch.setattr(cli, "utc_now", lambda: NOW)


def test_the_ingest_command_logs_one_summary_line(
    migrated: Schema, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    use(monkeypatch, migrated, FakeDiscovery(spread(300, WHOLE.start, WHOLE.end) + undated(3)))
    with caplog.at_level(logging.INFO):
        assert cli.main(["ingest"]) == 0
    assert ("succeeded: 13 windows (0 split), 15 calls; reported 300, fetched 300, unique 303 "
            "(3 undated); 0 changes") in caplog.text


def test_the_ingest_command_exits_quietly_when_another_run_holds_the_lock(
    migrated: Schema, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    use(monkeypatch, migrated, FakeDiscovery([]))
    with direct_connect(migrated.url) as holder, caplog.at_level(logging.INFO):
        holder.execute("SELECT pg_advisory_lock(%s)", (INGEST_LOCK.key,))
        assert cli.main(["ingest"]) == 0
    assert "another ingest is running" in caplog.text
