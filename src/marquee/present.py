"""Presentation for the dashboard. Pure: plain values in, display strings out.

Never colour alone: every problem state has a word (FAILED, WARNING, PARTIAL, LATE, STALE), so the
page reads correctly in a screenshot, in greyscale, or for someone colour-blind.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

LA = ZoneInfo("America/Los_Angeles")
SCOPE_LABEL = ("Ticketmaster LA market (DMA 324) · Music · next 90 days · "
               "face-value data, no resale prices")
FRESH_INTERVALS = 1.5  # under 1.5 schedule intervals since the last good run: fresh
LATE_INTERVALS = 3  # under 3: late; beyond: stale


@dataclass(frozen=True)
class Freshness:
    level: str  # fresh | late | stale | none
    word: str  # always shown, so the colour is never the only signal


def number(value: int | float | None) -> str:
    return "-" if value is None else f"{value:,.0f}"


def la_time(instant: datetime) -> str:
    """'Oct 6, 1:19 PM PDT'. The zone is shown because 1:30 AM happens twice on Nov 1."""
    local = instant.astimezone(LA)
    return f"{local:%b} {local.day}, {_clock(local.time())} {local:%Z}"


def la_date(day: date | None) -> str:
    return "TBA" if day is None else f"{day:%b} {day.day}"


def ago(then: datetime, now: datetime) -> str:
    seconds = int((now - then).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return _plural(seconds // 60, "minute") + " ago"
    if seconds < 48 * 3600:
        return _plural(seconds // 3600, "hour") + " ago"
    return _plural(seconds // 86400, "day") + " ago"


def freshness(last_success: datetime | None, now: datetime, *, interval_minutes: int) -> Freshness:
    if last_success is None:
        return Freshness("none", "NO RUNS YET")
    intervals = (now - last_success).total_seconds() / 60 / interval_minutes
    if intervals < FRESH_INTERVALS:
        return Freshness("fresh", "Fresh")
    if intervals < LATE_INTERVALS:
        return Freshness("late", "LATE")
    return Freshness("stale", "STALE")


def completeness_sentence(reported: int, received: int) -> str:
    """The demo's headline: what the API says exists, what we hold, and the gap."""
    text = (f"{number(reported)} reported · {number(received)} received · "
            f"{number(max(0, reported - received))} missing")
    if received > reported:
        text += f" ({number(received - reported)} more than the total: drift)"
    return text


def change_text(field: str, old: str | None, new: str | None) -> str:
    return f"{_value(field, old)} → {_value(field, new)}"


def result_word(passed: bool, severity: str) -> str:
    if passed:
        return "passed"
    return "FAILED" if severity == "error" else "WARNING"


def run_status_word(status: str) -> str:
    return {"partial": "PARTIAL", "failed": "FAILED"}.get(status, status)


def _value(field: str, value: str | None) -> str:
    if field == "local_date":
        return la_date(date.fromisoformat(value) if value else None)
    if field == "local_time":
        return _clock(time.fromisoformat(value)) if value else "TBA"
    if field == "public_sale_start":
        return la_time(datetime.fromisoformat(value.replace("Z", "+00:00"))) if value else "none"
    return value if value else "none"


def _clock(t: time) -> str:
    hour = t.hour % 12 or 12
    return f"{hour}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}" if n == 1 else f"{n} {unit}s"


def checks_summary(failed: int, warnings: int) -> str:
    return f"{failed} failed · {_plural(warnings, 'warning')}"


def event_time(t: time | None) -> str:
    return "TBA" if t is None else _clock(t)


_FIELD_LABELS = {"status": "Status", "local_date": "Date", "local_time": "Time",
                 "venue": "Venue", "public_sale_start": "Public onsale"}


def field_label(field: str) -> str:
    return _FIELD_LABELS.get(field, field)


def megabytes(size: int) -> str:
    return f"{size / 1024 ** 2:,.1f} MB"
