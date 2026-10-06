"""Fetch every page of a date window, and split windows that are over the cap (PLAN §4).

All API traffic goes through TicketmasterClient. Window arithmetic lives in windows.py (pure).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from marquee.tm_client import ApiError, Page, TicketmasterClient
from marquee.windows import Window, api_time

# The final query (api-notes §2-§4): the Ticketmaster LA market (DMA 324), Music segment.
BASE_QUERY: Mapping[str, str] = {"dmaId": "324", "segmentId": "KZFzniwnSyZfZ7v7nJ"}
PAGE_SIZE = 200  # the API's maximum (api-notes §5)
CAP = 1_000  # documented paging limit; the observed 1,200 is a margin we don't rely on
PAGING_TOO_DEEP = "DIS1035"


@dataclass(frozen=True)
class WindowResult:
    window: Window
    pages: tuple[Page, ...]  # page 0 first
    calls: int  # API calls spent on this window, including retries and any refused page
    stopped_by: str | None  # set when the API refused to page deeper

    @property
    def reported(self) -> int:
        return int(self.pages[0].body["page"]["totalElements"])

    @property
    def event_ids(self) -> list[str]:
        return [str(e["id"]) for page in self.pages for e in events(page)]

    @property
    def fetched(self) -> int:
        return len(self.event_ids)

    @property
    def over_cap(self) -> bool:
        return self.reported > CAP


def fetch_window(
    client: TicketmasterClient,
    base: Mapping[str, str],
    window: Window,
    *,
    first_page: Page | None = None,
    calls_before: int | None = None,
) -> WindowResult:
    """Every page of one window. Stops, and says so, if the API refuses to page deeper.

    `first_page` lets a caller that already fetched page 0 (to decide whether to split) reuse it;
    `calls_before` is the client's call count before that page, so `calls` stays honest.
    """
    start_calls = client.calls if calls_before is None else calls_before
    page_0 = first_page if first_page is not None else client.search_events(
        _params(base, window, 0)
    )
    pages = [page_0]
    stopped_by = None
    # An empty result has totalPages 0 (api-notes §5): page 0 was all there is.
    for number in range(1, int(page_0.body["page"]["totalPages"])):
        try:
            pages.append(client.search_events(_params(base, window, number)))
        except ApiError as exc:
            if exc.code != PAGING_TOO_DEEP:
                raise
            stopped_by = f"{exc.code} at page {number}"
            break
    return WindowResult(window, tuple(pages), client.calls - start_calls, stopped_by)


def events(page: Page) -> list[dict[str, Any]]:
    embedded = page.body.get("_embedded")
    found = embedded.get("events") if isinstance(embedded, dict) else None
    return found if isinstance(found, list) else []


def _params(base: Mapping[str, str], window: Window, page: int) -> dict[str, str]:
    return {
        **base,
        "startDateTime": api_time(window.start),
        "endDateTime": api_time(window.end),
        "size": str(PAGE_SIZE),
        "sort": "id,asc",
        "page": str(page),
    }
