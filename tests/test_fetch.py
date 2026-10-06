"""Paging and adaptive splitting against a fake Discovery API (tests/fake_discovery.py)."""

from datetime import UTC, date, datetime

import httpx
import pytest
from fake_discovery import FakeDiscovery, FakeEvent, client_for, spread

from marquee.fetch import BASE_QUERY, CAP, PAGE_SIZE, RangeResult, fetch_range, fetch_window
from marquee.tm_client import ApiError
from marquee.windows import Window, api_time, local_midnight, plan_range, plan_windows, split

NOW = datetime(2026, 10, 6, 15, 22, 49, tzinfo=UTC)
WHOLE = plan_range(NOW)
WINDOWS = plan_windows(NOW)


# --- One window, paged to the end (the brute force) ------------------------------------------

def test_brute_force_on_1276_events_gets_1200_and_says_why_it_stopped() -> None:
    api = FakeDiscovery(spread(1276, WHOLE.start, WHOLE.end))
    result = fetch_window(client_for(api), BASE_QUERY, WHOLE)
    assert result.reported == 1276
    assert result.fetched == 1200
    assert len(set(result.event_ids)) == 1200
    assert result.stopped_by == "DIS1035 at page 6"
    assert result.calls == 7 == len(api.requests)
    assert result.over_cap


def test_every_page_is_fetched_when_the_window_fits() -> None:
    api = FakeDiscovery(spread(450, WHOLE.start, WHOLE.end))
    result = fetch_window(client_for(api), BASE_QUERY, WHOLE)
    assert (result.reported, result.fetched, result.calls) == (450, 450, 3)
    assert result.stopped_by is None
    assert not result.over_cap


def test_an_empty_window_costs_one_call_and_is_not_an_error() -> None:
    # The real API answers an empty search with totalPages 0 and no _embedded (api-notes §5).
    api = FakeDiscovery([])
    result = fetch_window(client_for(api), BASE_QUERY, WHOLE)
    assert (result.reported, result.fetched, result.calls) == (0, 0, 1)
    assert result.stopped_by is None


def test_requests_carry_the_query_window_page_size_and_sort() -> None:
    api = FakeDiscovery(spread(450, WHOLE.start, WHOLE.end))
    fetch_window(client_for(api), BASE_QUERY, WHOLE)
    sent = [r.url.params for r in api.requests]
    assert [p["page"] for p in sent] == ["0", "1", "2"]
    for p in sent:
        assert p["dmaId"] == "324"
        assert p["segmentId"] == "KZFzniwnSyZfZ7v7nJ"
        assert (p["startDateTime"], p["endDateTime"]) == (api_time(WHOLE.start),
                                                          api_time(WHOLE.end))
        assert (p["size"], p["sort"]) == (str(PAGE_SIZE), "id,asc")


def test_events_exactly_on_both_edges_are_included() -> None:
    api = FakeDiscovery([FakeEvent("on-start", WHOLE.start), FakeEvent("on-end", WHOLE.end)])
    assert fetch_window(client_for(api), BASE_QUERY, WHOLE).fetched == 2


def test_other_api_errors_still_fail_fast() -> None:
    fault = {"faultstring": "Invalid ApiKey", "detail": {"errorcode": "oauth.v2.InvalidApiKey"}}

    def unauthorised(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"fault": fault})

    with pytest.raises(ApiError):
        fetch_window(client_for(httpx.MockTransport(unauthorised)), BASE_QUERY, WHOLE)


# --- Many windows, split while over the cap ------------------------------------------------------

def leaves(window: Window) -> list[Window]:
    halves = split(window)
    return [window] if halves is None else leaves(halves[0]) + leaves(halves[1])


def assert_covers_the_range(result: RangeResult) -> None:
    windows = [w.window for w in result.final]
    assert windows[0].start == WHOLE.start and windows[-1].end == WHOLE.end
    assert all(a.end == b.start for a, b in zip(windows, windows[1:], strict=False))


def test_spread_events_need_one_call_per_window_and_no_splits() -> None:
    api = FakeDiscovery(spread(1276, WHOLE.start, WHOLE.end))
    r = fetch_range(client_for(api), BASE_QUERY, WINDOWS)
    assert (len(r.final), r.splits, r.probe_calls, r.probe_events) == (13, 0, 0, 0)
    assert r.calls == 13 == len(api.requests)
    assert r.reported == r.fetched == 1276
    assert len(r.unique_ids) == 1276
    assert_covers_the_range(r)


def test_a_dense_week_is_split_until_every_window_fits() -> None:
    dense = spread(2500, WINDOWS[2].start, WINDOWS[2].end, prefix="dense")
    api = FakeDiscovery(dense + spread(300, WHOLE.start, WHOLE.end))
    r = fetch_range(client_for(api), BASE_QUERY, WINDOWS)
    assert r.splits >= 1
    assert all(w.reported <= CAP for w in r.final)
    assert len(r.unique_ids) == 2800
    assert_covers_the_range(r)
    # The run's numbers come from final windows only, so reported and fetched compare like with
    # like. Probe pages are counted apart: their events reappear in the child windows.
    assert r.reported == r.fetched
    assert r.probe_calls == r.splits == len(r.probes)
    assert r.probe_events == sum(p.fetched for p in r.probes) > 0
    assert {i for p in r.probes for i in p.event_ids} <= r.unique_ids
    assert r.calls == len(api.requests)


def test_one_day_over_the_cap_is_fetched_as_far_as_possible_and_flagged() -> None:
    busy = spread(1050, local_midnight(date(2026, 11, 1)), local_midnight(date(2026, 11, 2)),
                  prefix="busy")
    r = fetch_range(client_for(FakeDiscovery(busy)), BASE_QUERY, WINDOWS)
    over = r.over_cap
    assert [(w.window.first_day, w.window.days) for w in over] == [(date(2026, 11, 1), 1)]
    assert over[0].fetched == 1050  # like the real API, the fake still serves page 5
    assert len(r.unique_ids) == 1050
    assert_covers_the_range(r)


def test_an_event_on_a_window_edge_is_fetched_twice_but_counted_once() -> None:
    edge = FakeEvent("on-the-edge", WINDOWS[1].start)
    r = fetch_range(client_for(FakeDiscovery([edge])), BASE_QUERY, WINDOWS)
    assert (r.reported, r.fetched, len(r.unique_ids)) == (2, 2, 1)


def test_both_one_thirty_a_m_on_fall_back_night_are_fetched() -> None:
    # 01:30 happens twice in Los Angeles on 2026-11-01: 08:30Z (PDT) and 09:30Z (PST).
    twice = [FakeEvent("first-0130", datetime(2026, 11, 1, 8, 30, tzinfo=UTC)),
             FakeEvent("second-0130", datetime(2026, 11, 1, 9, 30, tzinfo=UTC))]
    days = [leaf for w in WINDOWS for leaf in leaves(w)]
    r = fetch_range(client_for(FakeDiscovery(twice)), BASE_QUERY, days)
    assert r.unique_ids == {"first-0130", "second-0130"}
    nov_1 = next(w for w in r.final if w.window.first_day == date(2026, 11, 1))
    assert set(nov_1.event_ids) == {"first-0130", "second-0130"}


def test_empty_windows_cost_one_call_each() -> None:
    r = fetch_range(client_for(FakeDiscovery([])), BASE_QUERY, WINDOWS)
    assert (r.calls, r.reported, r.fetched, len(r.unique_ids)) == (13, 0, 0, 0)


def test_a_lower_split_threshold_splits_ordinary_weeks() -> None:
    api = FakeDiscovery(spread(2000, WHOLE.start, WHOLE.end))  # about 155 a week, 22 a day
    r = fetch_range(client_for(api), BASE_QUERY, WINDOWS, split_threshold=100)
    assert r.splits > 0
    assert all(w.reported <= 100 for w in r.final)
    assert len(r.unique_ids) == 2000
    assert r.reported == r.fetched
    assert r.over_cap == []  # over_cap is about the API's paging cap, not the split threshold
    assert r.calls == len(api.requests)
    assert_covers_the_range(r)
