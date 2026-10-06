"""The pure half of the migration runner: which files to apply, and when to refuse. No database."""

import hashlib
from pathlib import Path

import pytest

from marquee.db import (
    INGEST_LOCK,
    MIGRATE_LOCK,
    MIGRATIONS_DIR,
    Migration,
    MigrationError,
    plan_migrations,
    read_migrations,
)


def mig(name: str, body: str = "SELECT 1;") -> Migration:
    return Migration(name=name, sha256=hashlib.sha256(body.encode()).hexdigest(), sql=body)


A = mig("001_init.sql")
B = mig("002_more.sql", "SELECT 2;")


def test_nothing_applied_returns_everything_in_filename_order() -> None:
    assert plan_migrations([B, A], {}) == [A, B]


def test_applied_files_are_skipped() -> None:
    assert plan_migrations([A, B], {A.name: A.sha256}) == [B]


def test_everything_applied_returns_nothing() -> None:
    assert plan_migrations([A, B], {A.name: A.sha256, B.name: B.sha256}) == []


def test_an_edited_applied_file_is_refused() -> None:
    with pytest.raises(MigrationError, match=r"001_init\.sql.*changed"):
        plan_migrations([A], {A.name: "0" * 64})


def test_an_applied_file_that_disappeared_is_refused() -> None:
    with pytest.raises(MigrationError, match=r"000_gone\.sql"):
        plan_migrations([A], {"000_gone.sql": "0" * 64, A.name: A.sha256})


def test_a_new_file_sorting_before_an_applied_one_is_refused() -> None:
    # Applying it now would run it after 002 even though its name says it comes first.
    with pytest.raises(MigrationError, match=r"001_init\.sql"):
        plan_migrations([A, B], {B.name: B.sha256})


def test_read_migrations_reads_sql_files_in_order_with_their_hash(tmp_path: Path) -> None:
    (tmp_path / "002_b.sql").write_text("SELECT 2;")
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    (tmp_path / "notes.txt").write_text("not a migration")
    found = read_migrations(tmp_path)
    assert [m.name for m in found] == ["001_a.sql", "002_b.sql"]
    assert found[0].sha256 == hashlib.sha256(b"SELECT 1;").hexdigest()
    assert found[0].sql == "SELECT 1;"


def test_an_empty_migrations_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="no migrations"):
        read_migrations(tmp_path)


def test_the_real_migrations_start_with_001_init() -> None:
    assert read_migrations(MIGRATIONS_DIR)[0].name == "001_init.sql"


def test_lock_ids_are_named_and_distinct() -> None:
    assert MIGRATE_LOCK.name == "marquee.migrate"
    assert INGEST_LOCK.name == "marquee.ingest"
    assert MIGRATE_LOCK.key != INGEST_LOCK.key
