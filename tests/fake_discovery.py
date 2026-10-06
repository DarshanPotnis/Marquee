"""A fake Discovery API for tests: synthetic events, the real API's observed rules.

Rules mirrored from docs/api-notes.md:
- startDateTime and endDateTime are both inclusive (§7, Phase 3 probe).
- totalPages is ceil(total / size); an empty result has totalPages 0 and no _embedded (§5).
- page * size beyond 1,000 returns 400 DIS1035; page 5 at size 200 is still served (§5).
- sort=id,asc orders by event id.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from marquee.tm_client import TicketmasterClient

API_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class FakeEvent:
    id: str
    starts_at: datetime  # the UTC instant the API filters on


def spread(n: int, start: datetime, end: datetime, prefix: str = "evt") -> list[FakeEvent]:
    """n events evenly spaced inside (start, end), at whole seconds like real dateTimes."""
    step = (end - start) / (n + 1)
    return [FakeEvent(f"{prefix}{i:05d}", (start + step * (i + 1)).replace(microsecond=0))
            for i in range(n)]


class FakeDiscovery:
    def __init__(self, events: list[FakeEvent]) -> None:
        self.events = sorted(events, key=lambda e: e.id)
        self.requests: list[httpx.Request] = []
        self.available = 4_000

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.available -= 1
        q = request.url.params
        size, page = int(q.get("size", "20")), int(q.get("page", "0"))
        if page * size > 1000:
            return httpx.Response(400, json={"errors": [{
                "code": "DIS1035",
                "detail": "API Limits Exceeded: Max paging depth exceeded. "
                          "(page * size) must be less than 1,000",
                "status": "400 BAD_REQUEST"}]})
        start = _parse(q.get("startDateTime"))
        end = _parse(q.get("endDateTime"))
        matching = [e for e in self.events
                    if (start is None or start <= e.starts_at)
                    and (end is None or e.starts_at <= end)]
        chunk = matching[page * size:(page + 1) * size]
        body: dict[str, object] = {
            "_links": {},
            "page": {"size": size, "totalElements": len(matching),
                     "totalPages": math.ceil(len(matching) / size), "number": page},
        }
        if chunk:
            body["_embedded"] = {"events": [
                {"id": e.id, "name": f"Synthetic {e.id}",
                 "dates": {"start": {"dateTime": e.starts_at.strftime(API_FORMAT)}}}
                for e in chunk]}
        return httpx.Response(200, json=body,
                              headers={"Rate-Limit-Available": str(self.available)})


def client_for(api: httpx.MockTransport | FakeDiscovery) -> TicketmasterClient:
    """A real TicketmasterClient over a fake transport, with time that never actually waits."""
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    transport = api if isinstance(api, httpx.MockTransport) else httpx.MockTransport(api)
    return TicketmasterClient("synthetic-test-key", transport=transport,
                              clock=lambda: clock[0], sleep=sleep, random=lambda: 1.0)


def _parse(value: str | None) -> datetime | None:
    return None if value is None else datetime.strptime(value, API_FORMAT).replace(tzinfo=UTC)

