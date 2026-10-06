"""Settings come from an injected mapping, so these tests never read the real environment."""

import dataclasses
from pathlib import Path

import pytest

from marquee.config import ConfigError, load_settings, settings_from_environment

DB_URL = "postgresql://marquee:s3cret-pw@db.example.test/marquee"


def test_defaults_with_only_the_database_url() -> None:
    s = load_settings({"DATABASE_URL": DB_URL})
    assert s.database_url == DB_URL
    assert s.tm_api_key is None
    assert s.raw_retention_days == 3
    assert s.schedule_interval_minutes == 60


@pytest.mark.parametrize("env", [{}, {"DATABASE_URL": ""}, {"DATABASE_URL": "   "}])
def test_missing_database_url_names_the_variable(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        load_settings(env)


def test_values_are_trimmed() -> None:
    # The real .env is written as `KEY = value`; stray spaces must not reach URLs or keys.
    s = load_settings({"DATABASE_URL": f"  {DB_URL} ", "TM_API_KEY": " abc123 ",
                       "RAW_RETENTION_DAYS": " 7 ", "SCHEDULE_INTERVAL_MINUTES": " 180 "})
    assert s.database_url == DB_URL
    assert s.tm_api_key == "abc123"
    assert s.raw_retention_days == 7
    assert s.schedule_interval_minutes == 180


def test_blank_values_fall_back_to_defaults() -> None:
    s = load_settings({"DATABASE_URL": DB_URL, "TM_API_KEY": "  ", "RAW_RETENTION_DAYS": "",
                       "SCHEDULE_INTERVAL_MINUTES": " "})
    assert s.tm_api_key is None
    assert s.raw_retention_days == 3
    assert s.schedule_interval_minutes == 60


@pytest.mark.parametrize("value", ["1", "14"])
def test_retention_accepts_1_to_14(value: str) -> None:
    assert load_settings({"DATABASE_URL": DB_URL, "RAW_RETENTION_DAYS": value}).raw_retention_days \
        == int(value)


@pytest.mark.parametrize("value", ["0", "15", "-3", "abc", "3.5"])
def test_retention_outside_1_to_14_is_rejected(value: str) -> None:
    with pytest.raises(ConfigError, match="RAW_RETENTION_DAYS"):
        load_settings({"DATABASE_URL": DB_URL, "RAW_RETENTION_DAYS": value})


@pytest.mark.parametrize("value", ["0", "-5", "abc", "1.5"])
def test_schedule_interval_must_be_a_positive_whole_number(value: str) -> None:
    with pytest.raises(ConfigError, match="SCHEDULE_INTERVAL_MINUTES"):
        load_settings({"DATABASE_URL": DB_URL, "SCHEDULE_INTERVAL_MINUTES": value})


def test_repr_and_str_hide_the_api_key_and_database_password() -> None:
    s = load_settings({"DATABASE_URL": DB_URL, "TM_API_KEY": "abc123secretkey"})
    for text in (repr(s), str(s)):
        assert "abc123secretkey" not in text
        assert "s3cret-pw" not in text


def test_settings_are_frozen() -> None:
    s = load_settings({"DATABASE_URL": DB_URL})
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.raw_retention_days = 9  # type: ignore[misc]


def test_environment_variables_override_the_dotenv_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"DATABASE_URL = {DB_URL}\nRAW_RETENTION_DAYS=5\n")
    for name in ("DATABASE_URL", "TM_API_KEY", "SCHEDULE_INTERVAL_MINUTES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RAW_RETENTION_DAYS", "7")
    s = settings_from_environment(dotenv)
    assert s.database_url == DB_URL
    assert s.raw_retention_days == 7
