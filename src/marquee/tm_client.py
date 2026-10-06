"""Ticketmaster Discovery API client: pacing, safe retries, a budget guard and no key leaks.

The rules are PLAN §6 plus what Phase 0 observed (docs/api-notes.md):
- 400s cost quota but carry no Rate-Limit headers, so the client counts every call itself.
- Errors come in two shapes: Discovery `{"errors": [...]}` and gateway `{"fault": {...}}`.
- The documented quota-exceeded 429 echoes the API key in its faultstring.
"""

from __future__ import annotations

import logging
import random as _random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://app.ticketmaster.com"
EVENTS_PATH = "/discovery/v2/events.json"

# Timeouts, all in one place. Phase 0: a full 200-event page (about 2.3 MB) took 0.57-0.95 s.
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 30.0
WRITE_TIMEOUT_S = 5.0
POOL_TIMEOUT_S = 5.0
TIMEOUTS = httpx.Timeout(
    connect=CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S, write=WRITE_TIMEOUT_S, pool=POOL_TIMEOUT_S
)

MIN_INTERVAL_S = 0.25  # at most 4 requests a second; the documented limit is 5
MAX_ATTEMPTS = 5
BACKOFF_BASE_S = 1.0
MAX_RETRY_AFTER_S = 60.0  # a longer wait is the next scheduled run's job, not this one's
DEFAULT_RESERVE = 200  # calls left untouched for manual runs
QUOTA_NEAR_ZERO = 10  # an unlabelled 429 with this few calls left means the day's quota is gone

# 429 fault codes we rely on (docs/api-notes.md §10). Documented, not yet observed.
QUOTA_VIOLATION = "policies.ratelimit.QuotaViolation"  # Ticketmaster docs: daily quota gone
SPIKE_ARREST_VIOLATION = "policies.ratelimit.SpikeArrestViolation"  # Apigee docs: per-second


class TicketmasterError(RuntimeError):
    """Base class. Messages never contain the API key."""


class ApiError(TicketmasterError):
    """A response retrying can't fix: our request or our key is wrong."""

    def __init__(self, message: str, *, status: int, code: str | None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class RetriesExhausted(TicketmasterError):
    """A failure that might have passed didn't, within MAX_ATTEMPTS or the Retry-After cap."""


class BudgetExhausted(TicketmasterError):
    """Stop cleanly: the reserve is reached or the daily quota is gone. The run ends partial."""


@dataclass(frozen=True)
class Page:
    params: dict[str, str]  # exactly what was asked for, never the key; saved with the raw body
    http_status: int
    body: dict[str, Any]
    rate_limit_available: int | None
    raw: bytes  # the response body exactly as received (after undoing the transfer gzip)


@dataclass(frozen=True)
class _Retry:
    problem: str
    wait_s: float


class TicketmasterClient:
    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        random: Callable[[], float] = _random.random,
        reserve: int = DEFAULT_RESERVE,
        base_url: str = BASE_URL,
    ) -> None:
        key = api_key.strip()
        if not key:
            raise ValueError("api_key is empty")
        self._key = key
        self._http = httpx.Client(base_url=base_url, timeout=TIMEOUTS, transport=transport)
        self._clock, self._sleep, self._random = clock, sleep, random
        self._reserve = reserve
        self._last_start: float | None = None
        self.calls = 0  # requests sent; every one costs quota, errors included
        self.retries = 0
        self.budget_left: int | None = None  # last Rate-Limit-Available minus calls sent since
        self.quota_resets_at: datetime | None = None
        # httpx logs every request URL at INFO, and the URL carries the key.
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)

    def __repr__(self) -> str:
        return (
            f"TicketmasterClient(calls={self.calls}, retries={self.retries}, "
            f"budget_left={self.budget_left})"
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> TicketmasterClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def search_events(self, params: Mapping[str, str]) -> Page:
        if any(name.lower() == "apikey" for name in params):
            raise ValueError("params must not contain apikey: they are saved with the raw response")
        wanted = dict(params)
        last_problem = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            outcome = self._attempt(wanted, attempt)
            if isinstance(outcome, Page):
                return outcome
            last_problem = outcome.problem
            if attempt == MAX_ATTEMPTS:
                break
            self.retries += 1
            log.warning(
                "ticketmaster: %s (attempt %d of %d); retrying in %.1f s",
                last_problem, attempt, MAX_ATTEMPTS, outcome.wait_s,
            )
            self._sleep(outcome.wait_s)
        raise RetriesExhausted(f"gave up after {MAX_ATTEMPTS} attempts; last: {last_problem}")

    def _attempt(self, params: dict[str, str], attempt: int) -> Page | _Retry:
        self._guard_budget()
        self._pace()
        self.calls += 1
        if self.budget_left is not None:
            self.budget_left -= 1
        try:
            resp = self._http.get(EVENTS_PATH, params={**params, "apikey": self._key})
        except (httpx.TransportError, httpx.DecodingError) as exc:
            # Only the type name: httpx messages can include the URL, which carries the key.
            return _Retry(type(exc).__name__, self._backoff(attempt))
        available = self._read_rate_headers(resp)
        if resp.status_code == 200:
            body = _usable_body(resp)
            if body is None:
                # Usually a passing glitch; it must never reach the transform as if it were data.
                return _Retry("HTTP 200 with an unusable body (not JSON, or no page object)",
                              self._backoff(attempt))
            return Page(params=params, http_status=200, body=body, rate_limit_available=available,
                        raw=resp.content)
        code, detail = _error_code_and_detail(resp)
        if resp.status_code == 429 and self._quota_is_gone(code):
            resets = self.quota_resets_at.isoformat() if self.quota_resets_at else "unknown"
            raise BudgetExhausted(
                f"daily quota exhausted (HTTP 429, {code or 'no fault code'}); resets at {resets}"
            )
        if resp.status_code == 429 or resp.status_code >= 500:
            problem = f"HTTP {resp.status_code}" + (f" {code}" if code else "")
            return _Retry(self._redact(problem), self._retry_wait(resp, attempt))
        raise ApiError(
            self._redact(f"HTTP {resp.status_code} {code or 'no code'}: {detail or 'no detail'}"),
            status=resp.status_code,
            code=code,
        )

    def _guard_budget(self) -> None:
        if self.budget_left is not None and self.budget_left <= self._reserve:
            raise BudgetExhausted(
                f"{self.budget_left} calls left, at or below the reserve of {self._reserve}; "
                "stopping before spending it"
            )

    def _pace(self) -> None:
        now = self._clock()
        if self._last_start is not None:
            wait = MIN_INTERVAL_S - (now - self._last_start)
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last_start = now

    def _backoff(self, attempt: int) -> float:
        # Exponential with jitter: 0.5-1 s, then 1-2 s, 2-4 s, 4-8 s.
        return float(BACKOFF_BASE_S * 2 ** (attempt - 1) * (0.5 + 0.5 * self._random()))

    def _retry_wait(self, resp: httpx.Response, attempt: int) -> float:
        retry_after = _seconds(resp.headers.get("Retry-After"))
        if retry_after is None:
            return self._backoff(attempt)
        if retry_after > MAX_RETRY_AFTER_S:
            raise RetriesExhausted(
                f"HTTP {resp.status_code}: server asked to wait {retry_after:.0f} s, more than the "
                f"{MAX_RETRY_AFTER_S:.0f} s cap; leaving it to the next run"
            )
        return retry_after

    def _quota_is_gone(self, code: str | None) -> bool:
        if code == QUOTA_VIOLATION:
            return True
        if code == SPIKE_ARREST_VIOLATION:
            return False
        # No code we recognise: retrying is only sensible if there is budget left to retry with.
        return self.budget_left is not None and self.budget_left <= QUOTA_NEAR_ZERO

    def _read_rate_headers(self, resp: httpx.Response) -> int | None:
        available = _whole(resp.headers.get("Rate-Limit-Available"))
        if available is not None:
            self.budget_left = available
        reset_ms = _whole(resp.headers.get("Rate-Limit-Reset"))  # epoch milliseconds
        if reset_ms is not None:
            self.quota_resets_at = datetime.fromtimestamp(reset_ms // 1000, UTC) + timedelta(
                milliseconds=reset_ms % 1000
            )
        return available

    def _redact(self, text: str) -> str:
        return text.replace(self._key, "***")


def _usable_body(resp: httpx.Response) -> dict[str, Any] | None:
    try:
        body = resp.json()
    except ValueError:  # includes JSONDecodeError and bad text encodings
        return None
    if isinstance(body, dict) and isinstance(body.get("page"), dict):
        return body
    return None


def _error_code_and_detail(resp: httpx.Response) -> tuple[str | None, str | None]:
    try:
        body = resp.json()
    except ValueError:
        return None, None
    if not isinstance(body, dict):
        return None, None
    errors = body.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return _text(errors[0].get("code")), _text(errors[0].get("detail"))
    fault = body.get("fault")
    if isinstance(fault, dict):
        detail = fault.get("detail")
        code = detail.get("errorcode") if isinstance(detail, dict) else None
        return _text(code), _text(fault.get("faultstring"))
    return None, None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _whole(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _seconds(value: str | None) -> float | None:
    # Only the delta-seconds form. An HTTP-date Retry-After falls back to normal backoff.
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    return seconds if seconds is not None and seconds >= 0 else None
