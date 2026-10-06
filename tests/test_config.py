"""Settings come from an injected mapping, so these tests never read the real environment."""

import dataclasses
from pathlib import Path

import pytest

from marquee.config import (
    ConfigError,
    load_settings,
    load_test_database_url,
    read_environment,
    settings_from_environment,
)

DB_URL = "postgresql://marquee:s3cret-pw@db.example.test/marquee"
ELSEWHERE_URLS = [
    "jdbc:postgresql://localhost/otherdb",
    "postgresql://other:0ther-pw@elsewhere.test/otherdb",
]


def test_defaults_with_only_the_database_url() -> None:
    s = load_settings({"MARQUEE_DATABASE_URL": DB_URL})
    assert s.database_url == DB_URL
    assert s.tm_api_key is None
    assert s.raw_retention_days == 3
    assert s.schedule_interval_minutes == 60


@pytest.mark.parametrize(
    "env", [{}, {"MARQUEE_DATABASE_URL": ""}, {"MARQUEE_DATABASE_URL": "   "}]
)
def test_missing_database_url_names_the_variable(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="MARQUEE_DATABASE_URL"):
        load_settings(env)


def test_the_generic_database_url_is_never_read() -> None:
    # Other projects on this machine export DATABASE_URL; Marquee must not fall back to it.
    with pytest.raises(ConfigError, match="MARQUEE_DATABASE_URL"):
        load_settings({"DATABASE_URL": DB_URL})


def test_values_are_trimmed() -> None:
    # The real .env is written as `KEY = value`; stray spaces must not reach URLs or keys.
    s = load_settings({"MARQUEE_DATABASE_URL": f"  {DB_URL} ", "TM_API_KEY": " abc123 ",
                       "RAW_RETENTION_DAYS": " 7 ", "SCHEDULE_INTERVAL_MINUTES": " 180 "})
    assert s.database_url == DB_URL
    assert s.tm_api_key == "abc123"
    assert s.raw_retention_days == 7
    assert s.schedule_interval_minutes == 180


def test_blank_values_fall_back_to_defaults() -> None:
    s = load_settings({"MARQUEE_DATABASE_URL": DB_URL, "TM_API_KEY": "  ",
                       "RAW_RETENTION_DAYS": "", "SCHEDULE_INTERVAL_MINUTES": " "})
    assert s.tm_api_key is None
    assert s.raw_retention_days == 3
    assert s.schedule_interval_minutes == 60


@pytest.mark.parametrize("value", ["1", "14"])
def test_retention_accepts_1_to_14(value: str) -> None:
    s = load_settings({"MARQUEE_DATABASE_URL": DB_URL, "RAW_RETENTION_DAYS": value})
    assert s.raw_retention_days == int(value)


@pytest.mark.parametrize("value", ["0", "15", "-3", "abc", "3.5"])
def test_retention_outside_1_to_14_is_rejected(value: str) -> None:
    with pytest.raises(ConfigError, match="RAW_RETENTION_DAYS"):
        load_settings({"MARQUEE_DATABASE_URL": DB_URL, "RAW_RETENTION_DAYS": value})


@pytest.mark.parametrize("value", ["0", "-5", "abc", "1.5"])
def test_schedule_interval_must_be_a_positive_whole_number(value: str) -> None:
    with pytest.raises(ConfigError, match="SCHEDULE_INTERVAL_MINUTES"):
        load_settings({"MARQUEE_DATABASE_URL": DB_URL, "SCHEDULE_INTERVAL_MINUTES": value})


def test_repr_and_str_hide_the_api_key_and_database_password() -> None:
    s = load_settings({"MARQUEE_DATABASE_URL": DB_URL, "TM_API_KEY": "abc123secretkey"})
    for text in (repr(s), str(s)):
        assert "abc123secretkey" not in text
        assert "s3cret-pw" not in text


def test_settings_are_frozen() -> None:
    s = load_settings({"MARQUEE_DATABASE_URL": DB_URL})
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.raw_retention_days = 9  # type: ignore[misc]


@pytest.mark.parametrize("url", ["postgresql://u@h/db", "postgres://u@h/db"])
def test_postgres_urls_are_accepted(url: str) -> None:
    assert load_settings({"MARQUEE_DATABASE_URL": url}).database_url == url


@pytest.mark.parametrize(
    "url",
    [
        "jdbc:postgresql://localhost/otherdb",
        "mysql://u:s3cret-pw@h/db",
        "host=h password=s3cret-pw",
    ],
)
def test_a_non_postgres_database_url_is_refused_without_echoing_it(url: str) -> None:
    with pytest.raises(ConfigError, match="MARQUEE_DATABASE_URL") as err:
        load_settings({"MARQUEE_DATABASE_URL": url})
    assert "s3cret-pw" not in str(err.value)
    assert "otherdb" not in str(err.value)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ("DATABASE_URL", "TEST_DATABASE_URL", "MARQUEE_DATABASE_URL",
                 "MARQUEE_TEST_DATABASE_URL", "TM_API_KEY", "RAW_RETENTION_DAYS",
                 "SCHEDULE_INTERVAL_MINUTES"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize("elsewhere", ELSEWHERE_URLS)
def test_a_shell_database_url_pointing_elsewhere_is_ignored_completely(
    tmp_path: Path, clean_env: pytest.MonkeyPatch, elsewhere: str
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"MARQUEE_DATABASE_URL = {DB_URL}\n")
    clean_env.setenv("DATABASE_URL", elsewhere)
    clean_env.setenv("TEST_DATABASE_URL", elsewhere)
    s = settings_from_environment(dotenv)
    assert s.database_url == DB_URL


@pytest.mark.parametrize("elsewhere", ELSEWHERE_URLS)
def test_a_shell_database_url_alone_is_not_a_fallback(
    tmp_path: Path, clean_env: pytest.MonkeyPatch, elsewhere: str
) -> None:
    clean_env.setenv("DATABASE_URL", elsewhere)
    with pytest.raises(ConfigError, match="MARQUEE_DATABASE_URL is not set"):
        settings_from_environment(tmp_path / "missing.env")


def test_dotenv_supplies_what_the_environment_lacks(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"MARQUEE_DATABASE_URL = {DB_URL}\nRAW_RETENTION_DAYS=5\n")
    s = settings_from_environment(dotenv)
    assert s.database_url == DB_URL
    assert s.raw_retention_days == 5


def test_environment_alone_works_without_a_dotenv_file(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    # CI has no .env; repository secrets arrive as environment variables.
    clean_env.setenv("MARQUEE_DATABASE_URL", DB_URL)
    assert settings_from_environment(tmp_path / "missing.env").database_url == DB_URL


def test_a_marquee_variable_set_differently_in_environment_and_dotenv_is_refused(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"MARQUEE_DATABASE_URL={DB_URL}\n")
    clean_env.setenv("MARQUEE_DATABASE_URL", "postgresql://other:0ther-pw@elsewhere.test/otherdb")
    with pytest.raises(ConfigError, match="MARQUEE_DATABASE_URL") as err:
        settings_from_environment(dotenv)
    assert "s3cret-pw" not in str(err.value)
    assert "0ther-pw" not in str(err.value)


def test_the_same_value_in_both_places_is_fine(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"MARQUEE_DATABASE_URL = {DB_URL}\n")
    clean_env.setenv("MARQUEE_DATABASE_URL", DB_URL)
    assert settings_from_environment(dotenv).database_url == DB_URL


def test_a_blank_dotenv_value_does_not_clash(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"MARQUEE_DATABASE_URL={DB_URL}\nTM_API_KEY=\n")
    clean_env.setenv("TM_API_KEY", "from-ci-secret")
    assert settings_from_environment(dotenv).tm_api_key == "from-ci-secret"


def test_read_environment_merges_without_clashes(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("MARQUEE_TEST_DATABASE_URL=postgresql://t@h/test\n")
    clean_env.setenv("TM_API_KEY", "k")
    env = read_environment(dotenv)
    assert env["MARQUEE_TEST_DATABASE_URL"] == "postgresql://t@h/test"
    assert env["TM_API_KEY"] == "k"


def test_clashes_outside_the_requested_names_are_ignored(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        f"MARQUEE_DATABASE_URL={DB_URL}\nMARQUEE_TEST_DATABASE_URL=postgresql://t@h/test\n"
    )
    clean_env.setenv("MARQUEE_DATABASE_URL", "postgresql://other@elsewhere.test/otherdb")
    env = read_environment(dotenv, names=["MARQUEE_TEST_DATABASE_URL"])
    assert env == {"MARQUEE_TEST_DATABASE_URL": "postgresql://t@h/test"}


def test_settings_ignore_a_clash_on_a_variable_they_do_not_read(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"MARQUEE_DATABASE_URL={DB_URL}\nOTHER_PROJECT_SETTING=a\n")
    clean_env.setenv("OTHER_PROJECT_SETTING", "b")
    assert settings_from_environment(dotenv).database_url == DB_URL


# --- Guards: tests can never touch production, and advisory locks always get a direct connection.

PROD = (
    "postgresql://app:s3cret-pw@ep-prod-111111.us-east-2.aws.neon.tech/neondb"
    "?sslmode=require&channel_binding=require"
)
TEST = "postgresql://app:t3st-pw@ep-dev-222222.us-east-2.aws.neon.tech/neondb?sslmode=require"
POOLED = "postgresql://app:s3cret-pw@ep-prod-111111-pooler.us-east-2.aws.neon.tech/neondb"


def test_a_test_database_url_equal_to_production_is_refused_by_the_app() -> None:
    with pytest.raises(ConfigError, match="MARQUEE_TEST_DATABASE_URL") as err:
        load_settings({"MARQUEE_DATABASE_URL": PROD, "MARQUEE_TEST_DATABASE_URL": PROD})
    assert "MARQUEE_DATABASE_URL" in str(err.value)
    assert "s3cret-pw" not in str(err.value)


def test_a_test_database_url_equal_to_production_is_refused_by_the_tests() -> None:
    with pytest.raises(ConfigError, match="MARQUEE_TEST_DATABASE_URL") as err:
        load_test_database_url({"MARQUEE_DATABASE_URL": PROD, "MARQUEE_TEST_DATABASE_URL": PROD})
    assert "s3cret-pw" not in str(err.value)


def test_the_same_database_written_differently_is_still_refused() -> None:
    # Same host (any case), default port spelled out, same database; other user, reordered params.
    same_db = (
        "postgresql://someone_else:x@EP-PROD-111111.us-east-2.aws.neon.tech:5432/neondb"
        "?channel_binding=require&sslmode=require"
    )
    with pytest.raises(ConfigError, match="MARQUEE_TEST_DATABASE_URL"):
        load_test_database_url({"MARQUEE_DATABASE_URL": PROD, "MARQUEE_TEST_DATABASE_URL": same_db})
    with pytest.raises(ConfigError, match="MARQUEE_TEST_DATABASE_URL"):
        load_settings({"MARQUEE_DATABASE_URL": PROD, "MARQUEE_TEST_DATABASE_URL": same_db})


def test_a_separate_test_database_is_accepted() -> None:
    env = {"MARQUEE_DATABASE_URL": PROD, "MARQUEE_TEST_DATABASE_URL": TEST}
    assert load_settings(env).database_url == PROD
    assert load_test_database_url(env) == TEST


def test_the_test_database_works_without_production_configured() -> None:
    # CI sets only the test URL.
    assert load_test_database_url({"MARQUEE_TEST_DATABASE_URL": TEST}) == TEST


def test_an_unset_test_database_url_is_none() -> None:
    assert load_test_database_url({"MARQUEE_DATABASE_URL": PROD}) is None


def test_a_pooled_production_url_is_refused() -> None:
    with pytest.raises(ConfigError, match="MARQUEE_DATABASE_URL.*pooler") as err:
        load_settings({"MARQUEE_DATABASE_URL": POOLED})
    assert "s3cret-pw" not in str(err.value)


def test_a_pooled_test_url_is_refused() -> None:
    with pytest.raises(ConfigError, match="MARQUEE_TEST_DATABASE_URL.*pooler") as err:
        load_test_database_url({"MARQUEE_TEST_DATABASE_URL": POOLED})
    assert "s3cret-pw" not in str(err.value)


def test_a_non_postgres_test_url_is_refused() -> None:
    with pytest.raises(ConfigError, match="MARQUEE_TEST_DATABASE_URL"):
        load_test_database_url({"MARQUEE_TEST_DATABASE_URL": "jdbc:postgresql://localhost/x"})
