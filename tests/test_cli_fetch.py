"""The read-only `brute-force` and `fetch-windows` commands, against the fake Discovery API."""

import logging
from datetime import UTC, datetime

import pytest
from fake_discovery import FakeDiscovery, client_for, spread

import marquee.__main__ as cli
from marquee.config import load_settings
from marquee.windows import api_time, plan_range

START = "2026-10-06T15:22:49Z"
WHOLE = plan_range(datetime(2026, 10, 6, 15, 22, 49, tzinfo=UTC))


def use(monkeypatch: pytest.MonkeyPatch, api: FakeDiscovery, *, key: str | None = "k") -> None:
    env = {"MARQUEE_DATABASE_URL": "postgresql://u@h/db"} | ({"TM_API_KEY": key} if key else {})
    settings = load_settings(env)
    monkeypatch.setattr(cli, "settings_from_environment", lambda: settings)
    monkeypatch.setattr(cli, "make_client", lambda s: client_for(api))


def test_brute_force_reports_what_one_search_loses(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    api = FakeDiscovery(spread(1276, WHOLE.start, WHOLE.end))
    use(monkeypatch, api)
    with caplog.at_level(logging.INFO):
        assert cli.main(["brute-force", "--start", START]) == 0
    assert ("reported 1276, fetched 1200 (1200 unique), lost 76; "
            "stopped by DIS1035 at page 6; 7 calls") in caplog.text
    first = api.requests[0].url.params
    assert (first["startDateTime"], first["endDateTime"]) == (START, api_time(WHOLE.end))


def test_fetch_commands_need_the_api_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    use(monkeypatch, FakeDiscovery([]), key=None)
    assert cli.main(["brute-force"]) == 2
    assert "TM_API_KEY" in caplog.text


def test_a_malformed_start_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["brute-force", "--start", "2026-10-06 15:22"])
    assert "--start" in capsys.readouterr().err
