"""Fetch every page of a date window, and split windows that are over the cap (PLAN §4).

All API traffic goes through TicketmasterClient. Window arithmetic lives in windows.py (pure).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from marquee.tm_client import ApiError, Page, TicketmasterClient
from marquee.windows import Window, api_time, split

# The final query (api-notes §2-§4): the Ticketmaster LA market (DMA 324), Music segment.
BASE_QUERY: Mapping[str, str] = {"dmaId": "324", "segmentId": "KZFzniwnSyZfZ7v7nJ"}
PAGE_SIZE = 200  # the API's maximum (api-notes §5)
CAP = 1_000  # documented paging limit; the observed 1,200 is a margin we don't rely on
PAGING_TOO_DEEP = "DIS1035"

# Called with every page the moment it arrives (probe pages too), so ingest can save it at once.
OnPage = Callable[["Window | None", Page], None]


@dataclass(frozen=True)
class WindowResult:
    window: Window | None  # None for an undated search (the TBA/TBD calls)
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
    window: Window | None,
    *,
    first_page: Page | None = None,
    calls_before: int | None = None,
    on_page: OnPage | None = None,
) -> WindowResult:
    """Every page of one window (or of an undated search, with window None).

    Stops, and says so, if the API refuses to page deeper. `first_page` lets a caller that
    already fetched page 0 (to decide whether to split) reuse it, and that caller has already
    passed it to on_page; `calls_before` is the client's call count before that page.
    """
    start_calls = client.calls if calls_before is None else calls_before
    if first_page is None:
        page_0 = client.search_events(_params(base, window, 0))
        if on_page is not None:
            on_page(window, page_0)
    else:
        page_0 = first_page
    pages = [page_0]
    stopped_by = None
    # An empty result has totalPages 0 (api-notes §5): page 0 was all there is.
    for number in range(1, int(page_0.body["page"]["totalPages"])):
        try:
            page = client.search_events(_params(base, window, number))
        except ApiError as exc:
            if exc.code != PAGING_TOO_DEEP:
                raise
            stopped_by = f"{exc.code} at page {number}"
            break
        pages.append(page)
        if on_page is not None:
            on_page(window, page)
    return WindowResult(window, tuple(pages), client.calls - start_calls, stopped_by)


@dataclass(frozen=True)
class RangeResult:
    """A windowed fetch. The run's numbers come from the final windows only.

    A window that turned out to be over the cap was split; its page 0 (the probe) is kept, because
    its events are real and get saved, but they reappear in the child windows. Counting probes
    apart keeps "reported vs fetched" like with like, and stops the duplicates from looking as if
    we fetched more events than exist.
    """

    final: tuple[WindowResult, ...]  # in date order; together they cover the range
    probes: tuple[WindowResult, ...]  # page 0 of each window that was split
    calls: int  # every API call, probes and retries included

    @property
    def reported(self) -> int:
        return sum(w.reported for w in self.final)

    @property
    def fetched(self) -> int:  # events received in final windows, edge overlaps included
        return sum(w.fetched for w in self.final)

    @property
    def unique_ids(self) -> set[str]:
        return {i for w in self.final for i in w.event_ids}

    @property
    def splits(self) -> int:
        return len(self.probes)

    @property
    def probe_calls(self) -> int:
        return sum(p.calls for p in self.probes)

    @property
    def probe_events(self) -> int:
        return sum(p.fetched for p in self.probes)

    @property
    def over_cap(self) -> list[WindowResult]:
        return [w for w in self.final if w.over_cap]


def fetch_range(
    client: TicketmasterClient,
    base: Mapping[str, str],
    windows: Sequence[Window],
    *,
    split_threshold: int = CAP,
    on_page: OnPage | None = None,
) -> RangeResult:
    """Fetch every window, halving any whose page 0 reports more than `split_threshold`.

    Splitting stops at one local day. `over_cap` stays tied to CAP, the API's paging limit,
    because that is the risk of losing data; a lower threshold only forces more splitting.
    """
    start_calls = client.calls
    final: list[WindowResult] = []
    probes: list[WindowResult] = []
    todo = deque(windows)
    while todo:
        window = todo.popleft()
        before = client.calls
        page_0 = client.search_events(_params(base, window, 0))
        if on_page is not None:
            on_page(window, page_0)
        reported = int(page_0.body["page"]["totalElements"])
        halves = split(window) if reported > split_threshold else None
        if halves is None:
            final.append(fetch_window(client, base, window, first_page=page_0,
                                      calls_before=before, on_page=on_page))
        else:
            probes.append(WindowResult(window, (page_0,), client.calls - before, None))
            todo.extendleft(reversed(halves))  # keep date order
    return RangeResult(tuple(final), tuple(probes), client.calls - start_calls)


def events(page: Page) -> list[dict[str, Any]]:
    embedded = page.body.get("_embedded")
    found = embedded.get("events") if isinstance(embedded, dict) else None
    return found if isinstance(found, list) else []


def _params(base: Mapping[str, str], window: Window | None, page: int) -> dict[str, str]:
    dates = {} if window is None else {
        "startDateTime": api_time(window.start), "endDateTime": api_time(window.end)}
    return {**base, **dates, "size": str(PAGE_SIZE), "sort": "id,asc", "page": str(page)}
