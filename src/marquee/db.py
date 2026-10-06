"""Postgres access: connections, named advisory locks and the migration runner.

`plan_migrations` is pure. Everything else here touches the database or the filesystem.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import psycopg
from psycopg.rows import TupleRow

Connection = psycopg.Connection[TupleRow]

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


@dataclass(frozen=True)
class AdvisoryLock:
    name: str
    key: int  # the bigint Postgres locks on; arbitrary, but unique per lock


MIGRATE_LOCK = AdvisoryLock("marquee.migrate", 7_301_001)
INGEST_LOCK = AdvisoryLock("marquee.ingest", 7_301_002)


class LockHeld(RuntimeError):
    """Another session holds the advisory lock, so this one must not run."""


class MigrationError(RuntimeError):
    """Migrations on disk and in the database disagree, or one failed and was rolled back."""


@dataclass(frozen=True)
class Migration:
    name: str
    sha256: str
    sql: str


@dataclass(frozen=True)
class MigrateResult:
    applied: tuple[str, ...]
    already_applied: int


class DatabaseConnectError(RuntimeError):
    """Connecting failed. The message has the database password removed."""


def connect(url: str) -> Connection:
    # Autocommit: advisory locks belong to the session, and every write opens its own transaction.
    try:
        return psycopg.connect(url, autocommit=True)
    except psycopg.Error as exc:
        # libpq quotes the connection string in some errors, password included. `from None` keeps
        # the original exception (and the URL in it) out of the traceback.
        raise DatabaseConnectError(
            f"could not connect to the database ({type(exc).__name__}): "
            f"{_without_password(str(exc), url)}"
        ) from None


def _without_password(text: str, url: str) -> str:
    try:
        password = urlsplit(url).password
    except ValueError:  # unparseable URL: hide the whole thing rather than guess
        return text.replace(url, "<MARQUEE_DATABASE_URL>")
    for secret in {password, unquote(password)} if password else set():
        text = text.replace(secret, "***")
    return text


@contextmanager
def advisory_lock(conn: Connection, lock: AdvisoryLock) -> Iterator[None]:
    """Take a session-level advisory lock without waiting; raise LockHeld if it's taken."""
    row = conn.execute("SELECT pg_try_advisory_lock(%s)", (lock.key,)).fetchone()
    if row is None or not row[0]:
        raise LockHeld(f"another session holds advisory lock {lock.name!r} (key {lock.key})")
    try:
        yield
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (lock.key,))


def read_migrations(directory: Path) -> list[Migration]:
    files = sorted(directory.glob("*.sql"))
    if not files:
        raise MigrationError(f"no migrations found in {directory}")
    migrations = []
    for f in files:
        data = f.read_bytes()
        migrations.append(Migration(f.name, hashlib.sha256(data).hexdigest(), data.decode()))
    return migrations


def plan_migrations(available: Sequence[Migration], applied: Mapping[str, str]) -> list[Migration]:
    """Return the migrations still to apply, in order. Refuse anything that rewrites history."""
    by_name = {m.name: m for m in available}
    for name, sha256 in applied.items():
        if name not in by_name:
            raise MigrationError(f"{name} was applied but is missing from the migrations directory")
        if by_name[name].sha256 != sha256:
            raise MigrationError(f"{name} changed after it was applied; add a new migration")
    pending = sorted((m for m in available if m.name not in applied), key=lambda m: m.name)
    last_applied = max(applied, default=None)
    if pending and last_applied is not None and pending[0].name < last_applied:
        raise MigrationError(
            f"{pending[0].name} sorts before already-applied {last_applied}; rename it to sort last"
        )
    return pending


def migrate(conn: Connection, directory: Path = MIGRATIONS_DIR) -> MigrateResult:
    """Apply pending migrations under the migrate lock, each file in its own transaction."""
    available = read_migrations(directory)
    with advisory_lock(conn, MIGRATE_LOCK):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " filename text PRIMARY KEY,"
            " sha256 text NOT NULL,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        rows = conn.execute("SELECT filename, sha256 FROM schema_migrations").fetchall()
        pending = plan_migrations(available, {str(name): str(sha) for name, sha in rows})
        for m in pending:
            _apply(conn, m)
    return MigrateResult(tuple(m.name for m in pending), len(available) - len(pending))


def _apply(conn: Connection, m: Migration) -> None:
    try:
        with conn.transaction():
            # Bytes, because file contents aren't a LiteralString. No parameters are passed, so
            # psycopg sends the file as one simple query and it may hold several statements.
            conn.execute(m.sql.encode())
            conn.execute(
                "INSERT INTO schema_migrations (filename, sha256) VALUES (%s, %s)",
                (m.name, m.sha256),
            )
    except psycopg.Error as exc:
        raise MigrationError(f"{m.name} failed and was rolled back: {exc}") from exc
