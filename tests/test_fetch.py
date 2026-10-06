"""Paging and adaptive splitting against a fake Discovery API (tests/fake_discovery.py)."""

from datetime import UTC, datetime

import httpx
import pytest
from fake_discovery import FakeDiscovery, FakeEvent, client_for, spread

from marquee.fetch import BASE_QUERY, PAGE_SIZE, fetch_window
from marquee.tm_client import ApiError
from marquee.windows import api_time, plan_range

NOW = datetime(2026, 10, 6, 15, 22, 49, tzinfo=UTC)
WHOLE = plan_range(NOW)


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
