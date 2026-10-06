"""Settings from environment variables (.env locally, repository secrets in CI)."""

from __future__ import annotations

import os
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

MAX_RAW_RETENTION_DAYS = 14  # PLAN §8: never keep raw API data longer than this


class ConfigError(ValueError):
    """A setting is missing or invalid. Messages name the variable but never echo a secret."""


@dataclass(frozen=True)
class Settings:
    # repr=False keeps the key and the database password out of logs and tracebacks.
    database_url: str = field(repr=False)
    tm_api_key: str | None = field(default=None, repr=False)
    raw_retention_days: int = 3
    schedule_interval_minutes: int = 60


def load_settings(env: Mapping[str, str]) -> Settings:
    # Only the MARQUEE_ name: other projects on the same machine export a generic DATABASE_URL.
    database_url = _text(env, "MARQUEE_DATABASE_URL")
    if database_url is None:
        raise ConfigError("MARQUEE_DATABASE_URL is not set")
    _check_database_url("MARQUEE_DATABASE_URL", database_url)
    test_url = _text(env, "MARQUEE_TEST_DATABASE_URL")
    if test_url is not None and _same_database(test_url, database_url):
        raise ConfigError(_TEST_IS_PRODUCTION)
    return Settings(
        database_url=database_url,
        tm_api_key=_text(env, "TM_API_KEY"),
        raw_retention_days=_whole_number(
            env, "RAW_RETENTION_DAYS", default=3, low=1, high=MAX_RAW_RETENTION_DAYS
        ),
        schedule_interval_minutes=_whole_number(
            env, "SCHEDULE_INTERVAL_MINUTES", default=60, low=1
        ),
    )


def load_test_database_url(env: Mapping[str, str]) -> str | None:
    """The integration-test database URL, or None if unset. Never production, never pooled."""
    url = _text(env, "MARQUEE_TEST_DATABASE_URL")
    if url is None:
        return None
    _check_database_url("MARQUEE_TEST_DATABASE_URL", url)
    production = _text(env, "MARQUEE_DATABASE_URL")
    if production is not None and _same_database(url, production):
        raise ConfigError(_TEST_IS_PRODUCTION)
    return url


_TEST_IS_PRODUCTION = (
    "MARQUEE_TEST_DATABASE_URL points at the same database as MARQUEE_DATABASE_URL; tests create "
    "and drop schemas and take the migrate lock, so use a separate database (a Neon dev branch "
    "or the local container)"
)

SETTING_NAMES = (
    "MARQUEE_DATABASE_URL",
    "MARQUEE_TEST_DATABASE_URL",  # read only to refuse a test URL that is production
    "TM_API_KEY",
    "RAW_RETENTION_DAYS",
    "SCHEDULE_INTERVAL_MINUTES",
)


def settings_from_environment(dotenv_path: Path | None = None) -> Settings:
    return load_settings(read_environment(dotenv_path, names=SETTING_NAMES))


def read_environment(
    dotenv_path: Path | None = None, names: Collection[str] | None = None
) -> dict[str, str]:
    """Merge .env with the process environment, refusing to guess when they disagree.

    A variable exported by a shell profile would otherwise silently decide which database we
    touch. CI has no .env, so its secrets apply unopposed.
    Blank .env values count as unset. With `names`, only those variables are checked and returned.
    """
    path = dotenv_path or Path.cwd() / ".env"
    from_file = {k: v.strip() for k, v in dotenv_values(path).items() if v and v.strip()}
    from_env = dict(os.environ)
    if names is not None:
        from_file = {k: v for k, v in from_file.items() if k in names}
        from_env = {k: v for k, v in from_env.items() if k in names}
    clashes = sorted(
        k for k, v in from_file.items() if k in from_env and from_env[k].strip() != v
    )
    if clashes:
        raise ConfigError(
            f"{', '.join(clashes)} set differently in the shell environment and in {path.name}; "
            "unset one so it's clear which applies"
        )
    return from_file | from_env


def _check_database_url(name: str, url: str) -> None:
    # Messages never echo the URL: it may hold a password.
    if not url.startswith(("postgresql://", "postgres://")):
        raise ConfigError(f"{name} must be a postgresql:// or postgres:// URL")
    if "-pooler" in urlsplit(url).netloc.rpartition("@")[2].lower():
        # Advisory locks belong to one server session. A pooler can hand that session to another
        # client mid-run, so the lock would no longer keep two runs apart.
        raise ConfigError(
            f"{name} uses a pooled endpoint (-pooler in the host); advisory locks need a direct "
            "connection, so use the host without -pooler"
        )


def _same_database(a: str, b: str) -> bool:
    """Same host, port and database name, however the rest of the URL is written."""
    try:
        pa, pb = urlsplit(a), urlsplit(b)
        ident_a = ((pa.hostname or "").lower(), pa.port or 5432, pa.path.lstrip("/"))
        ident_b = ((pb.hostname or "").lower(), pb.port or 5432, pb.path.lstrip("/"))
    except ValueError:  # unparseable port or host: fall back to comparing the text
        return a == b
    return ident_a == ident_b


def _text(env: Mapping[str, str], name: str) -> str | None:
    value = (env.get(name) or "").strip()
    return value or None


def _whole_number(
    env: Mapping[str, str], name: str, *, default: int, low: int, high: int | None = None
) -> int:
    raw = _text(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None
    if value < low or (high is not None and value > high):
        bounds = f"between {low} and {high}" if high is not None else f"at least {low}"
        raise ConfigError(f"{name} must be {bounds}, got {value}")
    return value
