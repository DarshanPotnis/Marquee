"""A fake Discovery API for tests: synthetic events, the real API's observed rules.

Rules mirrored from docs/api-notes.md:
- startDateTime and endDateTime are both inclusive (§7, Phase 3 probe).
- totalPages is ceil(total / size); an empty result has totalPages 0 and no _embedded (§5).
- page * size beyond 1,000 returns 400 DIS1035; page 5 at size 200 is still served (§5).
- sort=id,asc orders by event id.
- Dated searches never return undated (TBA) events; includeTBA=only returns only those, and
  includeTBA=only together with includeTBD=only returns nothing (§8).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx

from marquee.tm_client import TicketmasterClient

API_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
LA = ZoneInfo("America/Los_Angeles")


@dataclass(frozen=True)
class FakeEvent:
    id: str
    starts_at: datetime | None  # the UTC instant the API filters on; None for an undated event
    status: str = "onsale"
    venue: str = "KovFAKE001"
    onsale: str | None = "2026-08-01T17:00:00Z"
    tbd: bool = False  # undated because the date is "to be decided" rather than "announced"


def spread(n: int, start: datetime, end: datetime, prefix: str = "evt") -> list[FakeEvent]:
    """n events evenly spaced inside (start, end), at whole seconds like real dateTimes."""
    step = (end - start) / (n + 1)
    return [FakeEvent(f"{prefix}{i:05d}", (start + step * (i + 1)).replace(microsecond=0))
            for i in range(n)]


def undated(n: int, prefix: str = "tba") -> list[FakeEvent]:
    return [FakeEvent(f"{prefix}{i:03d}", None, status="postponed") for i in range(n)]


class FakeDiscovery:
    def __init__(self, events: list[FakeEvent]) -> None:
        self.events = sorted(events, key=lambda e: e.id)
        self.requests: list[httpx.Request] = []
        self.sent: list[bytes] = []  # exact bytes of every 200 body, in order
        self.failures: dict[int, Callable[[], httpx.Response]] = {}  # 1-based request number
        self.available = 4_000
        self.over_report = 0  # windows claim this many more events than they serve
        self.total_extra = 0  # a size=1 count query (the whole-range total) claims this many more

    def update(self, event_id: str, **changes: object) -> None:
        self.events = [replace(e, **changes) if e.id == event_id else e  # type: ignore[arg-type]
                       for e in self.events]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.available -= 1
        if len(self.requests) in self.failures:
            return self.failures[len(self.requests)]()
        q = request.url.params
        size, page = int(q.get("size", "20")), int(q.get("page", "0"))
        if page * size > 1000:
            return httpx.Response(400, json={"errors": [{
                "code": "DIS1035",
                "detail": "API Limits Exceeded: Max paging depth exceeded. "
                          "(page * size) must be less than 1,000",
                "status": "400 BAD_REQUEST"}]})
        matching = self._matching(q)
        chunk = matching[page * size:(page + 1) * size]
        claimed = len(matching) + (self.total_extra if size == 1 else self.over_report)
        body: dict[str, object] = {
            "_links": {},
            "page": {"size": size, "totalElements": claimed,
                     "totalPages": math.ceil(len(matching) / size), "number": page},
        }
        if chunk:
            body["_embedded"] = {"events": [_event_json(e) for e in chunk]}
        content = json.dumps(body).encode()
        self.sent.append(content)
        return httpx.Response(200, content=content,
                              headers={"Content-Type": "application/json",
                                       "Rate-Limit-Available": str(self.available)})

    def _matching(self, q: httpx.QueryParams) -> list[FakeEvent]:
        tba, tbd = q.get("includeTBA"), q.get("includeTBD")
        if tba == "only" and tbd == "only":
            return []
        if tba == "only":
            return [e for e in self.events if e.starts_at is None and not e.tbd]
        if tbd == "only":
            return [e for e in self.events if e.starts_at is None and e.tbd]
        start, end = _parse(q.get("startDateTime")), _parse(q.get("endDateTime"))
        return [e for e in self.events if e.starts_at is not None
                and (start is None or start <= e.starts_at)
                and (end is None or e.starts_at <= end)]


def _event_json(e: FakeEvent) -> dict[str, object]:
    if e.starts_at is None:
        start: dict[str, object] = {"dateTBD": e.tbd, "dateTBA": not e.tbd, "timeTBA": False,
                                    "noSpecificTime": False}
    else:
        local = e.starts_at.astimezone(LA)
        start = {"localDate": local.date().isoformat(), "localTime": local.strftime("%H:%M:%S"),
                 "dateTime": e.starts_at.strftime(API_FORMAT), "dateTBD": False,
                 "dateTBA": False, "timeTBA": False, "noSpecificTime": False}
    return {
        "name": f"Synthetic {e.id}", "type": "event", "id": e.id,
        "url": f"https://www.ticketmaster.com/event/{e.id}",
        "sales": {"public": {"startDateTime": e.onsale} if e.onsale else {}},
        "dates": {"start": start, "timezone": "America/Los_Angeles",
                  "status": {"code": e.status}},
        "classifications": [{"primary": True, "segment": {"name": "Music"},
                             "genre": {"name": "Rock"}}],
        "_embedded": {
            "venues": [{"name": f"Venue {e.venue}", "id": e.venue,
                        "timezone": "America/Los_Angeles", "city": {"name": "Los Angeles"},
                        "state": {"stateCode": "CA"},
                        "location": {"latitude": "34.05000", "longitude": "-118.25000"}}],
            "attractions": [{"name": f"Act {e.id}", "id": f"K8v{e.id}"}],
        },
    }


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


def page_of(*events: FakeEvent) -> dict[str, object]:
    """A response body holding these events, as the fake API would send it."""
    return {"_links": {}, "_embedded": {"events": [_event_json(e) for e in events]},
            "page": {"size": 200, "totalElements": len(events), "totalPages": 1, "number": 0}}
