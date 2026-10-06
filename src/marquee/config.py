"""Settings from environment variables (.env locally, repository secrets in CI)."""

from __future__ import annotations

import os
from collections.abc import Mapping
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
    database_url = _text(env, "DATABASE_URL")
    if database_url is None:
        raise ConfigError("DATABASE_URL is not set")
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


def settings_from_environment(dotenv_path: Path | None = None) -> Settings:
    """Real environment variables win over .env, so CI secrets beat a stray local file."""
    from_file = dotenv_values(dotenv_path or Path.cwd() / ".env")
    merged = {k: v for k, v in from_file.items() if v is not None} | dict(os.environ)
    return load_settings(merged)


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
