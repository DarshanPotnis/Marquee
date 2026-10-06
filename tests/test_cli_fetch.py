"""The read-only `brute-force` and `fetch-windows` commands, against the fake Discovery API."""

import logging
from datetime import UTC, date, datetime

import pytest
from fake_discovery import FakeDiscovery, client_for, spread

import marquee.__main__ as cli
from marquee.config import load_settings
from marquee.windows import api_time, local_midnight, plan_range

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


def test_fetch_windows_proves_unique_ids_equal_the_reported_total(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    api = FakeDiscovery(spread(1276, WHOLE.start, WHOLE.end))
    use(monkeypatch, api)
    with caplog.at_level(logging.INFO):
        assert cli.main(["fetch-windows", "--start", START]) == 0
    assert "13 final windows, 0 split (0 probe calls, 0 probe events)" in caplog.text
    assert "final windows: reported 1276, fetched 1276, unique 1276" in caplog.text
    assert "whole range reported 1276 now; unique == reported: yes" in caplog.text
    assert "14 calls (13 for windows + 1 whole-range check); over-cap days: none" in caplog.text
    check = api.requests[-1].url.params
    assert (check["startDateTime"], check["endDateTime"], check["size"]) == \
        (START, api_time(WHOLE.end), "1")


def test_fetch_windows_exits_1_when_unique_ids_fall_short(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # One local day with 1,300 events: it can't be split and the API serves only 1,200 of them.
    busy = spread(1300, local_midnight(date(2026, 11, 1)), local_midnight(date(2026, 11, 2)))
    use(monkeypatch, FakeDiscovery(busy))
    with caplog.at_level(logging.INFO):
        assert cli.main(["fetch-windows", "--start", START]) == 1
    assert "unique == reported: NO (1200 of 1300)" in caplog.text
    assert "over-cap days: 2026-11-01 (1300 reported, stopped by DIS1035 at page 6)" in caplog.text
