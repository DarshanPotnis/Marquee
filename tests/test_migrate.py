"""The migration runner and `python -m marquee migrate` against a real Postgres.

Uses MARQUEE_TEST_DATABASE_URL and skips cleanly without it. Each test gets a throwaway schema.
"""

import logging
from pathlib import Path

import psycopg
import pytest
from conftest import Schema

import marquee.__main__ as cli
from marquee import db
from marquee.__main__ import main
from marquee.config import load_settings
from marquee.db import MIGRATE_LOCK, LockHeld, MigrationError, migrate

EXPECTED_TABLES = {
    "ingest_runs", "raw_responses", "venues", "attractions", "events", "event_attractions",
    "event_changes", "check_results", "schema_migrations",
}


def tables(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    ).fetchall()
    return {r[0] for r in rows}


def hold_migrate_lock(schema: Schema) -> psycopg.Connection:
    holder = psycopg.connect(schema.url, autocommit=True)
    holder.execute("SELECT pg_advisory_lock(%s)", (MIGRATE_LOCK.key,))
    return holder


def use_test_schema(schema: Schema, monkeypatch: pytest.MonkeyPatch) -> None:
    # Hand the CLI its settings directly, so these tests never read the real .env (and its
    # production MARQUEE_DATABASE_URL), and point its connections at the throwaway schema.
    settings = load_settings({"MARQUEE_DATABASE_URL": schema.url})
    monkeypatch.setattr(cli, "settings_from_environment", lambda: settings)
    monkeypatch.setattr(db, "connect", lambda url: schema.connect())


def test_fresh_migrate_creates_every_table(schema: Schema) -> None:
    with schema.connect() as conn:
        result = migrate(conn)
        assert result.applied == ("001_init.sql",)
        assert result.already_applied == 0
        assert tables(conn) == EXPECTED_TABLES


def test_second_run_applies_nothing(schema: Schema) -> None:
    with schema.connect() as conn:
        migrate(conn)
        before = tables(conn)
        again = migrate(conn)
        assert again.applied == ()
        assert again.already_applied == 1
        assert tables(conn) == before


def test_a_failing_migration_leaves_nothing_half_applied(schema: Schema, tmp_path: Path) -> None:
    (tmp_path / "001_ok.sql").write_text("CREATE TABLE ok_table (id int);")
    (tmp_path / "002_bad.sql").write_text("CREATE TABLE half_table (id int);\nSELEC broken;")
    with schema.connect() as conn:
        with pytest.raises(MigrationError, match=r"002_bad\.sql"):
            migrate(conn, tmp_path)
        assert "ok_table" in tables(conn)
        assert "half_table" not in tables(conn)
        recorded = [r[0] for r in conn.execute("SELECT filename FROM schema_migrations")]
        assert recorded == ["001_ok.sql"]


def test_an_edited_applied_migration_is_refused(schema: Schema, tmp_path: Path) -> None:
    f = tmp_path / "001_a.sql"
    f.write_text("CREATE TABLE a (id int);")
    with schema.connect() as conn:
        migrate(conn, tmp_path)
        f.write_text("CREATE TABLE a (id bigint);")
        with pytest.raises(MigrationError, match=r"001_a\.sql"):
            migrate(conn, tmp_path)


def test_event_changes_only_accepts_tracked_fields(schema: Schema) -> None:
    with schema.connect() as conn:
        migrate(conn)
        run = conn.execute(
            "INSERT INTO ingest_runs (source) VALUES ('ticketmaster') RETURNING run_id"
        ).fetchone()
        event = conn.execute(
            "INSERT INTO events (source, source_id, name)"
            " VALUES ('ticketmaster', 'synthetic-1', 'Synthetic Show') RETURNING event_id"
        ).fetchone()
        assert run is not None and event is not None
        insert = (
            "INSERT INTO event_changes (event_id, run_id, detected_at, field, old_value, new_value)"
            " VALUES (%s, %s, now(), %s, 'a', 'b')"
        )
        conn.execute(insert, (event[0], run[0], "status"))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert, (event[0], run[0], "price"))


def test_migrate_refuses_to_run_while_another_session_holds_the_lock(schema: Schema) -> None:
    with hold_migrate_lock(schema), schema.connect() as conn:
        with pytest.raises(LockHeld, match=r"marquee\.migrate"):
            migrate(conn)
        assert tables(conn) == set()


def test_cli_migrate_runs_twice_cleanly(
    schema: Schema, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    use_test_schema(schema, monkeypatch)
    with caplog.at_level(logging.INFO):
        assert main(["migrate"]) == 0
        assert main(["migrate"]) == 0
    assert "applied 1 (001_init.sql), already applied 0" in caplog.text
    assert "applied 0, already applied 1" in caplog.text


def test_cli_exits_with_a_clear_message_while_the_lock_is_held(
    schema: Schema, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    use_test_schema(schema, monkeypatch)
    with hold_migrate_lock(schema), caplog.at_level(logging.INFO):
        assert main(["migrate"]) == 1
    assert "marquee.migrate" in caplog.text
    assert "nothing was changed" in caplog.text.lower()
    with schema.connect() as conn:
        assert tables(conn) == set()
