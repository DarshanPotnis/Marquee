"""Date windows in Los Angeles calendar days, sent to the API as UTC instants. Pure: no I/O.

Why local days: a show "on Nov 1" is on the Los Angeles Nov 1, which is 25 hours long because
daylight saving ends that night. Events with no specific time land inside their local day
(api-notes §7). The API includes both window edges, so windows share edges: nothing falls into a
gap, and an event exactly on an edge is fetched twice and deduped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

LA = ZoneInfo("America/Los_Angeles")
RANGE_DAYS = 90
WINDOW_DAYS = 7
API_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"  # the only format the API accepts (api-notes §7)


@dataclass(frozen=True)
class Window:
    start: datetime  # UTC instant; the API includes it
    end: datetime  # UTC instant; the API includes it, and the next window starts here
    first_day: date  # Los Angeles calendar day the window starts in
    days: int  # Los Angeles calendar days covered; the first may be partial (it starts at now)

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("window edges need a timezone")
        if not self.start < self.end:
            raise ValueError("a window must end after it starts")
        if self.days < 1:
            raise ValueError("a window covers at least one day")


def local_midnight(day: date) -> datetime:
    """The UTC instant at which `day` begins in Los Angeles."""
    return datetime(day.year, day.month, day.day, tzinfo=LA).astimezone(UTC)


def plan_range(now: datetime, *, days: int = RANGE_DAYS) -> Window:
    """From now to the Los Angeles midnight that ends the `days`-th day, counting today."""
    _require_timezone(now)
    start = now.astimezone(UTC).replace(microsecond=0)
    today = start.astimezone(LA).date()
    return Window(start, local_midnight(today + timedelta(days=days)), today, days)


def plan_windows(
    now: datetime, *, days: int = RANGE_DAYS, window_days: int = WINDOW_DAYS
) -> list[Window]:
    whole = plan_range(now, days=days)
    windows = []
    for offset in range(0, days, window_days):
        n = min(window_days, days - offset)
        first = whole.first_day + timedelta(days=offset)
        start = whole.start if offset == 0 else local_midnight(first)
        windows.append(Window(start, local_midnight(first + timedelta(days=n)), first, n))
    return windows


def split(window: Window) -> tuple[Window, Window] | None:
    """Halve at a Los Angeles midnight. A single day can't be split."""
    if window.days < 2:
        return None
    left_days = window.days // 2
    middle_day = window.first_day + timedelta(days=left_days)
    middle = local_midnight(middle_day)
    return (
        Window(window.start, middle, window.first_day, left_days),
        Window(middle, window.end, middle_day, window.days - left_days),
    )


def api_time(instant: datetime) -> str:
    _require_timezone(instant)
    return instant.astimezone(UTC).strftime(API_TIME_FORMAT)


def _require_timezone(instant: datetime) -> None:
    if instant.tzinfo is None:
        raise ValueError("naive datetime: refusing to guess its timezone")
