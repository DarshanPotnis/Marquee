"""Prune: delete raw responses of whole runs older than RAW_RETENTION_DAYS (PLAN §8).

The most recent succeeded run's raw is always kept, so a rebuild has one complete run even if the
scheduler stopped for a week. Run records and check results are kept forever (they're tiny).
Uses MARQUEE_TEST_DATABASE_URL and skips cleanly without it.
"""

import logging
from datetime import UTC, datetime

import pytest
from conftest import Schema
from fake_discovery import FakeDiscovery, client_for, spread

import marquee.__main__ as cli
from marquee import db
from marquee.config import load_settings
from marquee.db import migrate
from marquee.ingest import ingest
from marquee.prune import prune
from marquee.windows import plan_range

NOW = datetime(2026, 10, 6, 15, 22, 49, tzinfo=UTC)
WHOLE = plan_range(NOW)


@pytest.fixture
def migrated(schema: Schema) -> Schema:
    with schema.connect() as conn:
        migrate(conn)
    return schema


def runs_aged(schema: Schema, *days_old: float, status: str = "succeeded") -> list[int]:
    """One real (fake-API) ingest per age, then backdated by that many days.

    Ingest prunes at the end of every run; a 14-day retention here keeps that out of the way, so
    the only pruning is the one under test.
    """
    api = FakeDiscovery(spread(30, WHOLE.start, WHOLE.end))
    ids = []
    for days in days_old:
        with schema.connect() as conn:
            s = ingest(conn, client_for(api), now=NOW, retention_days=14)
            assert s is not None
            conn.execute("UPDATE ingest_runs SET started_at = now() - make_interval(secs => %s),"
                         " status = %s WHERE run_id = %s", (days * 86400, status, s.run_id))
            ids.append(s.run_id)
    return ids


def raw_runs(schema: Schema) -> list[int]:
    with schema.connect() as conn:
        return [r[0] for r in conn.execute("SELECT DISTINCT run_id FROM raw_responses ORDER BY 1")]


def test_raw_of_runs_older_than_retention_is_deleted(migrated: Schema) -> None:
    five, four, fresh = runs_aged(migrated, 5, 4, 0.1)
    with migrated.connect() as conn:
        r = prune(conn, retention_days=3)
    assert r.runs == (five, four)
    assert r.raw_rows == 30 and r.raw_bytes > 0
    assert r.kept_run == fresh
    assert raw_runs(migrated) == [fresh]


def test_the_latest_succeeded_run_is_kept_however_old(migrated: Schema) -> None:
    five, four = runs_aged(migrated, 5, 4)
    with migrated.connect() as conn:
        r = prune(conn, retention_days=3)
    assert (r.runs, r.kept_run) == ((five,), four)
    assert raw_runs(migrated) == [four]


def test_old_failed_runs_dont_count_as_the_one_to_keep(migrated: Schema) -> None:
    (good,) = runs_aged(migrated, 6)
    (bad,) = runs_aged(migrated, 5, status="failed")
    with migrated.connect() as conn:
        r = prune(conn, retention_days=3)
    assert (r.runs, r.kept_run) == ((bad,), good)


def test_a_dry_run_deletes_nothing_but_says_what_it_would(migrated: Schema) -> None:
    five, _fresh = runs_aged(migrated, 5, 0.1)
    with migrated.connect() as conn:
        r = prune(conn, retention_days=3, dry_run=True)
    assert r.runs == (five,) and r.raw_rows == 15
    assert len(raw_runs(migrated)) == 2


def test_run_records_and_check_results_are_kept(migrated: Schema) -> None:
    runs_aged(migrated, 5, 0.1)
    with migrated.connect() as conn:
        before = conn.execute("SELECT count(*), (SELECT count(*) FROM check_results)"
                              " FROM ingest_runs").fetchone()
        prune(conn, retention_days=3)
        after = conn.execute("SELECT count(*), (SELECT count(*) FROM check_results)"
                             " FROM ingest_runs").fetchone()
    assert before == after


def test_the_prune_command_dry_run_reports_and_keeps_everything(
    migrated: Schema, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runs_aged(migrated, 5, 0.1)
    settings = load_settings({"MARQUEE_DATABASE_URL": migrated.url, "RAW_RETENTION_DAYS": "3"})
    monkeypatch.setattr(cli, "settings_from_environment", lambda: settings)
    monkeypatch.setattr(db, "connect", lambda url: migrated.connect())
    with caplog.at_level(logging.INFO):
        assert cli.main(["prune", "--dry-run"]) == 0
    assert "would delete 15 raw responses from 1 run" in caplog.text
    assert len(raw_runs(migrated)) == 2
