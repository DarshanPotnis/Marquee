"""The Ticketmaster client against a scripted fake server. No network, no real waiting.

FakeTime records sleeps instead of sleeping. Server replays scripted responses (or raises scripted
errors) and records every request the client actually sent.
"""

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from marquee.tm_client import (
    CONNECT_TIMEOUT_S,
    MAX_ATTEMPTS,
    MAX_RETRY_AFTER_S,
    MIN_INTERVAL_S,
    QUOTA_VIOLATION,
    READ_TIMEOUT_S,
    SPIKE_ARREST_VIOLATION,
    ApiError,
    BudgetExhausted,
    RetriesExhausted,
    TicketmasterClient,
)

KEY = "test-key-SYNTHETIC-0123456789abcd"
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "events_page.json").read_text())
PARAMS = {"dmaId": "324", "segmentId": "KZFzniwnSyZfZ7v7nJ", "size": "200", "sort": "id,asc"}

Step = httpx.Response | Callable[[httpx.Request], httpx.Response]


class FakeTime:
    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Server:
    def __init__(self, *steps: Step) -> None:
        self.steps = list(steps)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.steps.pop(0)
        return step if isinstance(step, httpx.Response) else step(request)


def ok(available: int | None = 4_000, body: object = None) -> httpx.Response:
    headers = {"Rate-Limit": "5000", "Rate-Limit-Over": "0", "Rate-Limit-Reset": "1791385613158"}
    if available is not None:
        headers["Rate-Limit-Available"] = str(available)
    return httpx.Response(200, json=FIXTURE if body is None else body, headers=headers)


def fault(status: int, errorcode: str, faultstring: str = "", **headers: str) -> httpx.Response:
    body = {"fault": {"faultstring": faultstring, "detail": {"errorcode": errorcode}}}
    return httpx.Response(status, json=body, headers=headers)


def dis_error(code: str, detail: str) -> httpx.Response:
    body = {"errors": [{"_links": {"about": {"href": f"/discovery/v2/errors.html#{code}"}},
                        "code": code, "detail": detail, "status": "400 BAD_REQUEST"}]}
    return httpx.Response(400, json=body)


def raises(exc_type: type[httpx.TransportError]) -> Callable[[httpx.Request], httpx.Response]:
    # httpx's own messages often contain the URL, and the URL contains the key: copy that here.
    def step(request: httpx.Request) -> httpx.Response:
        raise exc_type(f"failed while fetching {request.url}", request=request)
    return step


def quota_429() -> httpx.Response:
    # The body Ticketmaster documents for an exhausted daily quota. It echoes the API key.
    return fault(429, QUOTA_VIOLATION,
                 f"Rate limit quota violation. Quota limit exceeded. Identifier : {KEY}")


def make_client(server: Server, fake: FakeTime, **kwargs: int) -> TicketmasterClient:
    return TicketmasterClient(KEY, transport=httpx.MockTransport(server), clock=fake.clock,
                              sleep=fake.sleep, random=lambda: 1.0, **kwargs)


# --- The happy path ---------------------------------------------------------------------------

def test_a_good_page_comes_back_with_key_free_params() -> None:
    server, fake = Server(ok(available=4_321)), FakeTime()
    client = make_client(server, fake)
    page = client.search_events(PARAMS)
    assert page.http_status == 200
    assert page.body["page"]["totalElements"] == 1
    assert page.params == PARAMS
    assert page.rate_limit_available == 4_321
    assert (client.calls, client.retries, client.budget_left) == (1, 0, 4_321)
    sent = server.requests[0]
    assert sent.url.path == "/discovery/v2/events.json"
    assert sent.url.params["apikey"] == KEY  # the key does go out, only on the wire


def test_the_quota_reset_time_is_read() -> None:
    client = make_client(Server(ok()), FakeTime())
    client.search_events(PARAMS)
    assert client.quota_resets_at == datetime(2026, 10, 7, 15, 6, 53, 158_000, tzinfo=UTC)


def test_an_empty_result_is_usable() -> None:
    empty = {"_links": {}, "page": {"size": 200, "totalElements": 0, "totalPages": 0, "number": 0}}
    server = Server(ok(body=empty))
    page = make_client(server, FakeTime()).search_events(PARAMS)
    assert page.body["page"]["totalElements"] == 0
    assert len(server.requests) == 1


def test_requests_use_the_named_timeouts_not_httpx_defaults() -> None:
    server = Server(ok())
    make_client(server, FakeTime()).search_events(PARAMS)
    sent = server.requests[0].extensions["timeout"]
    assert sent["connect"] == CONNECT_TIMEOUT_S
    assert sent["read"] == READ_TIMEOUT_S
    assert CONNECT_TIMEOUT_S != READ_TIMEOUT_S
    assert sent != httpx.Timeout(5.0).as_dict()  # httpx's default: 5 s for everything


# --- Retries: only for failures that can pass ----------------------------------------------------

def test_a_throttle_429_is_retried_after_backoff() -> None:
    server, fake = Server(fault(429, SPIKE_ARREST_VIOLATION, "Spike arrest violation"), ok()), \
        FakeTime()
    client = make_client(server, fake)
    assert client.search_events(PARAMS).http_status == 200
    assert (client.calls, client.retries) == (2, 1)
    assert fake.sleeps == [1.0]


def test_retry_after_is_honoured() -> None:
    server, fake = Server(fault(429, SPIKE_ARREST_VIOLATION, **{"Retry-After": "7"}), ok()), \
        FakeTime()
    make_client(server, fake).search_events(PARAMS)
    assert fake.sleeps == [7.0]


def test_a_retry_after_beyond_the_cap_gives_up_at_once() -> None:
    server, fake = Server(fault(429, SPIKE_ARREST_VIOLATION, **{"Retry-After": "3600"})), \
        FakeTime()
    with pytest.raises(RetriesExhausted, match="3600") as err:
        make_client(server, fake).search_events(PARAMS)
    assert str(int(MAX_RETRY_AFTER_S)) in str(err.value)
    assert len(server.requests) == 1
    assert fake.sleeps == []


def test_five_500s_give_up_after_exactly_five_attempts() -> None:
    server, fake = Server(*[httpx.Response(500) for _ in range(5)]), FakeTime()
    with pytest.raises(RetriesExhausted, match="5 attempts"):
        make_client(server, fake).search_events(PARAMS)
    assert len(server.requests) == MAX_ATTEMPTS == 5
    assert fake.sleeps == [1.0, 2.0, 4.0, 8.0]


def test_timeouts_and_connection_errors_are_retried() -> None:
    server, fake = Server(raises(httpx.ReadTimeout), raises(httpx.ConnectError), ok()), FakeTime()
    assert make_client(server, fake).search_events(PARAMS).http_status == 200
    assert len(server.requests) == 3
    assert fake.sleeps == [1.0, 2.0]


@pytest.mark.parametrize(
    "unusable",
    [
        httpx.Response(200, content=b"<html>upstream hiccup</html>"),  # not JSON
        httpx.Response(200, json={"_links": {}}),  # no page object
        httpx.Response(200, json={"page": "zero"}),  # page isn't an object
        httpx.Response(200, json=[1, 2, 3]),  # not a JSON object
    ],
)
def test_an_unusable_200_is_retried_and_never_returned(unusable: httpx.Response) -> None:
    server, fake = Server(unusable, ok()), FakeTime()
    page = make_client(server, fake).search_events(PARAMS)
    assert page.body == FIXTURE
    assert len(server.requests) == 2
    assert fake.sleeps == [1.0]


def test_unusable_200s_share_the_attempt_limit() -> None:
    server = Server(*[httpx.Response(200, content=b"not json") for _ in range(5)])
    with pytest.raises(RetriesExhausted, match="unusable"):
        make_client(server, FakeTime()).search_events(PARAMS)
    assert len(server.requests) == MAX_ATTEMPTS


# --- Fail fast: our bug or our key ---------------------------------------------------------------

def test_401_fails_fast_with_the_gateway_code() -> None:
    server, fake = Server(fault(401, "oauth.v2.InvalidApiKey", "Invalid ApiKey")), FakeTime()
    with pytest.raises(ApiError) as err:
        make_client(server, fake).search_events(PARAMS)
    assert (err.value.status, err.value.code) == (401, "oauth.v2.InvalidApiKey")
    assert len(server.requests) == 1
    assert fake.sleeps == []


def test_400_paging_too_deep_fails_fast() -> None:
    detail = "API Limits Exceeded: Max paging depth exceeded. (page * size) must be less than 1,000"
    server = Server(dis_error("DIS1035", detail))
    with pytest.raises(ApiError) as err:
        make_client(server, FakeTime()).search_events(PARAMS)
    assert (err.value.status, err.value.code) == (400, "DIS1035")
    assert len(server.requests) == 1


# --- The two kinds of 429 ----------------------------------------------------------------------

def test_a_quota_429_stops_the_run_instead_of_retrying() -> None:
    server, fake = Server(quota_429()), FakeTime()
    with pytest.raises(BudgetExhausted, match="quota"):
        make_client(server, fake).search_events(PARAMS)
    assert len(server.requests) == 1
    assert fake.sleeps == []


def test_an_unlabelled_429_reporting_zero_available_is_quota_exhaustion() -> None:
    server = Server(httpx.Response(429, headers={"Rate-Limit-Available": "0"}))
    with pytest.raises(BudgetExhausted):
        make_client(server, FakeTime()).search_events(PARAMS)
    assert len(server.requests) == 1


def test_an_unlabelled_429_with_the_estimate_near_zero_is_quota_exhaustion() -> None:
    server, fake = Server(ok(available=8), httpx.Response(429)), FakeTime()
    client = make_client(server, fake, reserve=0)
    client.search_events(PARAMS)
    with pytest.raises(BudgetExhausted):
        client.search_events(PARAMS)
    assert len(server.requests) == 2
    assert fake.sleeps == [MIN_INTERVAL_S]  # pacing only, no backoff


def test_an_unlabelled_429_with_budget_to_spare_is_a_throttle() -> None:
    server, fake = Server(ok(available=4_000), httpx.Response(429), ok()), FakeTime()
    client = make_client(server, fake)
    client.search_events(PARAMS)
    assert client.search_events(PARAMS).http_status == 200
    assert fake.sleeps == [MIN_INTERVAL_S, 1.0]


def test_an_unlabelled_429_with_no_estimate_yet_is_a_throttle() -> None:
    server, fake = Server(httpx.Response(429), ok()), FakeTime()
    assert make_client(server, fake).search_events(PARAMS).http_status == 200
    assert fake.sleeps == [1.0]


# --- Pacing ------------------------------------------------------------------------------------

def test_back_to_back_calls_are_spaced_at_least_250_ms() -> None:
    server, fake = Server(ok(), ok()), FakeTime()
    client = make_client(server, fake)
    client.search_events(PARAMS)
    client.search_events(PARAMS)
    assert MIN_INTERVAL_S >= 0.25
    assert fake.sleeps == [MIN_INTERVAL_S]


def test_no_pacing_wait_when_enough_time_has_passed() -> None:
    server, fake = Server(ok(), ok()), FakeTime()
    client = make_client(server, fake)
    client.search_events(PARAMS)
    fake.now += 1.0
    client.search_events(PARAMS)
    assert fake.sleeps == []


# --- Budget guard --------------------------------------------------------------------------------

def test_the_budget_guard_keeps_the_reserve_and_sends_nothing_once_reached() -> None:
    server = Server(ok(available=202), ok(available=201), ok(available=200))
    client = make_client(server, FakeTime(), reserve=200)
    for _ in range(3):
        client.search_events(PARAMS)
    with pytest.raises(BudgetExhausted, match="reserve"):
        client.search_events(PARAMS)
    assert len(server.requests) == 3
    assert client.budget_left == 200


def test_header_less_errors_still_count_against_the_budget() -> None:
    # Phase 0: 400s cost quota but carry no Rate-Limit headers, so the client must count them.
    bad = [dis_error("DIS1015", "Query param with date must be of valid format") for _ in range(3)]
    server = Server(ok(available=203), *bad)
    client = make_client(server, FakeTime(), reserve=200)
    client.search_events(PARAMS)
    for _ in range(3):
        with pytest.raises(ApiError):
            client.search_events(PARAMS)
    assert client.budget_left == 200
    with pytest.raises(BudgetExhausted):
        client.search_events(PARAMS)
    assert len(server.requests) == 4


# --- The key never leaks -------------------------------------------------------------------------

def test_the_key_never_reaches_logs_saved_params_or_repr(caplog: pytest.LogCaptureFixture) -> None:
    server = Server(raises(httpx.ConnectError), httpx.Response(500, text=f"echo {KEY}"), ok())
    client = make_client(server, FakeTime())
    with caplog.at_level(logging.DEBUG):
        page = client.search_events(PARAMS)
    assert len(caplog.records) >= 2  # the two retries were logged, so this check means something
    assert KEY not in caplog.text
    assert "apikey" not in page.params
    assert KEY not in repr(client)
    assert KEY not in str(client)


@pytest.mark.parametrize(
    ("steps", "error"),
    [
        ([fault(401, "oauth.v2.InvalidApiKey", f"Invalid ApiKey {KEY}")], ApiError),
        ([quota_429()], BudgetExhausted),
        ([raises(httpx.ConnectError)] * 5, RetriesExhausted),
        ([httpx.Response(500, text=f"echo {KEY}") for _ in range(5)], RetriesExhausted),
    ],
)
def test_error_messages_never_contain_the_key(steps: list[Step], error: type[Exception]) -> None:
    with pytest.raises(error) as err:
        make_client(Server(*steps), FakeTime()).search_events(PARAMS)
    assert KEY not in str(err.value)
    # httpx exceptions hold the full URL; none may ride along as a cause or context.
    assert err.value.__cause__ is None
    assert err.value.__context__ is None


@pytest.mark.parametrize("name", ["apikey", "APIKEY", "ApiKey"])
def test_the_key_cannot_be_smuggled_in_through_params(name: str) -> None:
    client = make_client(Server(), FakeTime())
    with pytest.raises(ValueError, match="apikey"):
        client.search_events({**PARAMS, name: "anything"})


def test_an_empty_key_is_refused() -> None:
    with pytest.raises(ValueError):
        TicketmasterClient("   ")
