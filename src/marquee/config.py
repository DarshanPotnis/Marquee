"""Settings from environment variables (.env locally, repository secrets in CI)."""

from __future__ import annotations

import os
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path

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
    if not database_url.startswith(("postgresql://", "postgres://")):
        # Never echo the value: it may hold a password.
        raise ConfigError("MARQUEE_DATABASE_URL must be a postgresql:// or postgres:// URL")
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


SETTING_NAMES = (
    "MARQUEE_DATABASE_URL", "TM_API_KEY", "RAW_RETENTION_DAYS", "SCHEDULE_INTERVAL_MINUTES"
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
