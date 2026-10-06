"""One ingest run end to end: fake API in, a throwaway Postgres schema out.

Uses MARQUEE_TEST_DATABASE_URL and skips cleanly without it.
"""

import gzip
import hashlib
import logging
from datetime import UTC, date, datetime

import httpx
import pytest
from conftest import Schema
from fake_discovery import FakeDiscovery, FakeEvent, client_for, page_of, spread, undated

import marquee.__main__ as cli
from marquee import db
from marquee.checks import CHECKS
from marquee.config import load_settings
from marquee.db import INGEST_LOCK, migrate
from marquee.db import connect as direct_connect
from marquee.ingest import RunSummary, ingest
from marquee.load import load_rows
from marquee.tm_client import QUOTA_VIOLATION
from marquee.transform import transform_page
from marquee.windows import api_time, local_midnight, plan_range

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
    # 13 windows + 1 whole-range total (a count, not data: not saved) + 2 undated calls.
    assert (s.windows, s.api_calls, s.reported_total, s.fetched_total) == (13, 16, 300, 300)
    assert (s.unique_events, s.undated_events, s.probe_calls, s.probe_events) == (303, 3, 0, 0)
    assert (s.changes, s.onsale_nulled) == (0, {})
    c = counts(migrated)
    assert (c["events"], c["venues"], c["attractions"], c["event_attractions"]) == \
        (303, 1, 303, 303)
    assert c["raw_responses"] == 15 == len(api.requests) - 1
    assert c["event_changes"] == 0
    assert query(migrated, "SELECT status, finished_at IS NOT NULL, api_calls, undated_events,"
                           " onsale_nulled FROM ingest_runs") == \
        [("succeeded", True, 16, 3, {})]
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
    # Every response except the whole-range count (size=1), which is a check, not data.
    saved = [body for req, body in zip(api.requests, api.sent, strict=True)
             if req.url.params.get("size") != "1"]
    assert len(rows) == len(saved)
    for (params, packed, sha, size), sent in zip(rows, saved, strict=True):
        assert gzip.decompress(packed) == sent  # type: ignore[arg-type]
        assert (sha, size) == (hashlib.sha256(sent).hexdigest(), len(sent))
        assert "apikey" not in {k.lower() for k in params}  # type: ignore[union-attr]


def test_probe_pages_are_saved_and_counted_apart(migrated: Schema) -> None:
    api = FakeDiscovery(spread(300, WHOLE.start, WHOLE.end))  # about 23 a week, 3 a day
    s = run(migrated, api, split_threshold=20)
    assert s is not None and s.probe_calls > 0 and s.probe_events > 0
    assert counts(migrated)["raw_responses"] == s.api_calls - 1 == len(api.requests) - 1
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
    assert ("succeeded: 13 windows (0 split), 16 calls; reported 300, fetched 300, unique 303 "
            "(3 undated); 0 changes") in caplog.text
    assert "checks: 7 run, 0 failed, 0 warnings; pruned 0 raw" in caplog.text


def test_the_ingest_command_exits_quietly_when_another_run_holds_the_lock(
    migrated: Schema, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    use(monkeypatch, migrated, FakeDiscovery([]))
    with direct_connect(migrated.url) as holder, caplog.at_level(logging.INFO):
        holder.execute("SELECT pg_advisory_lock(%s)", (INGEST_LOCK.key,))
        assert cli.main(["ingest"]) == 0
    assert "another ingest is running" in caplog.text


# --- Checks (PLAN §5) ----------------------------------------------------------------------------

def check_rows(schema: Schema, run_id: int) -> dict[str, tuple[object, ...]]:
    rows = query(schema, "SELECT check_name, passed, severity, observed, expected"
                         " FROM check_results WHERE run_id = %s", run_id)
    return {r[0]: r[1:] for r in rows}  # type: ignore[misc]


def test_a_run_writes_every_check_with_its_severity(migrated: Schema) -> None:
    api = FakeDiscovery(spread(300, WHOLE.start, WHOLE.end) + undated(3))
    s = run(migrated, api)
    assert s is not None and s.checks_failed == 0 and s.warnings == 0
    rows = check_rows(migrated, s.run_id)
    assert [(n, rows[n][1]) for n, _ in CHECKS] == list(CHECKS)
    assert all(passed for passed, *_ in rows.values())
    assert rows["unique_vs_total"][2:] == (300, 300)
    total_call = api.requests[13].url.params  # straight after the 13 windows
    assert (total_call["size"], total_call["endDateTime"]) == ("1", api_time(WHOLE.end))
    assert query(migrated, "SELECT checks_failed FROM ingest_runs") == [(0,)]


def test_an_over_reporting_window_fails_fetched_vs_reported(migrated: Schema) -> None:
    api = FakeDiscovery(spread(300, WHOLE.start, WHOLE.end))
    api.over_report = 5  # each window holds about 23 events, so the tolerance is 2
    s = run(migrated, api)
    assert s is not None and s.checks_failed >= 1
    assert check_rows(migrated, s.run_id)["fetched_vs_reported"][0] is False


def test_a_total_above_what_the_windows_return_fails_unique_vs_total(migrated: Schema) -> None:
    api = FakeDiscovery(spread(300, WHOLE.start, WHOLE.end))
    api.total_extra = 20  # tolerance for 320 is 3
    s = run(migrated, api)
    assert s is not None
    rows = check_rows(migrated, s.run_id)
    assert rows["unique_vs_total"][0] is False and rows["unique_vs_total"][2:] == (300, 320)
    assert rows["fetched_vs_reported"][0] is True


def test_a_crowded_day_fails_window_over_cap(migrated: Schema) -> None:
    busy = spread(1050, local_midnight(date(2026, 11, 1)), local_midnight(date(2026, 11, 2)))
    s = run(migrated, FakeDiscovery(busy))
    assert s is not None and check_rows(migrated, s.run_id)["window_over_cap"][0] is False


def test_a_sharp_drop_fails_volume_vs_baseline(migrated: Schema) -> None:
    with migrated.connect() as conn:
        for _ in range(3):
            conn.execute("INSERT INTO ingest_runs (source, status, unique_events)"
                         " VALUES ('ticketmaster', 'succeeded', 1000)")
    s = run(migrated, FakeDiscovery(spread(300, WHOLE.start, WHOLE.end)))
    assert s is not None
    assert check_rows(migrated, s.run_id)["volume_vs_baseline"][:2] == (False, "error")


def test_warnings_are_recorded_but_dont_fail_the_run(migrated: Schema) -> None:
    events = spread(20, WHOLE.start, WHOLE.end)
    events[0] = FakeEvent(events[0].id, events[0].starts_at, onsale="2027-06-01T17:00:00Z")
    s = run(migrated, FakeDiscovery(events))
    assert s is not None and s.checks_failed == 0 and s.warnings == 1
    assert check_rows(migrated, s.run_id)["implausible_onsales"][:2] == (False, "warning")


def test_an_incomplete_run_skips_the_checks(migrated: Schema) -> None:
    api = FakeDiscovery(spread(300, WHOLE.start, WHOLE.end))
    api.failures[5] = quota_429
    s = run(migrated, api)
    assert s is not None and s.status == "partial"
    assert check_rows(migrated, s.run_id) == {}


def test_each_ingest_prunes_raw_older_than_retention(migrated: Schema) -> None:
    api = FakeDiscovery(spread(50, WHOLE.start, WHOLE.end))
    old = run(migrated, api)
    assert old is not None
    with migrated.connect() as conn:  # make the first run 4 days old; a newer run will exist
        conn.execute("UPDATE ingest_runs SET started_at = now() - interval '4 days'")
    run(migrated, api)  # newest succeeded run: always kept
    newest = run(migrated, api)
    assert newest is not None and newest.pruned_raw == 15
    assert query(migrated, "SELECT count(*) FROM raw_responses WHERE run_id = %s", old.run_id) \
        == [(0,)]
    assert query(migrated, "SELECT pruned_raw FROM ingest_runs WHERE run_id = %s",
                 newest.run_id) == [(15,)]


# --- Exit codes: every kind of bad run shows red ------------------------------------------------

@pytest.mark.parametrize("problem", ["check", "partial", "failed"])
def test_the_ingest_command_exits_1_for_any_bad_run(
    migrated: Schema, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    api = FakeDiscovery(spread(300, WHOLE.start, WHOLE.end))
    if problem == "check":
        api.over_report = 5
    elif problem == "partial":
        api.failures[5] = quota_429
    else:
        for n in range(5, 10):
            api.failures[n] = lambda: httpx.Response(500)
    use(monkeypatch, migrated, api)
    assert cli.main(["ingest"]) == 1


def test_the_checks_command_reports_a_run(
    migrated: Schema, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    use(monkeypatch, migrated, FakeDiscovery(spread(50, WHOLE.start, WHOLE.end)))
    assert cli.main(["ingest"]) == 0
    with caplog.at_level(logging.INFO):
        assert cli.main(["checks"]) == 0
    assert "run 1: 7 checks, 0 failed, 0 warnings" in caplog.text
    assert "unique_vs_total: passed (error)" in caplog.text
