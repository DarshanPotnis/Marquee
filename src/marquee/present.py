"""Presentation for the dashboard. Pure: plain values in, display strings out.

Never colour alone: every problem state has a word (FAILED, WARNING, PARTIAL, LATE, STALE), so the
page reads correctly in a screenshot, in greyscale, or for someone colour-blind.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from marquee.runs import ABANDONED

LA = ZoneInfo("America/Los_Angeles")
SCOPE_LABEL = ("Ticketmaster LA market (DMA 324) · Music · next 90 days · "
               "face-value data, no resale prices")
FRESH_INTERVALS = 1.5  # under 1.5 schedule intervals since the last good run: fresh
LATE_INTERVALS = 3  # under 3: late; beyond: stale
MISSING = "—"


@dataclass(frozen=True)
class Freshness:
    level: str  # fresh | late | stale | none
    word: str  # always shown, so the colour is never the only signal


def number(value: int | float | None) -> str:
    return "-" if value is None else f"{value:,.0f}"


def la_time(instant: datetime, today: date | None = None) -> str:
    """'Oct 6, 1:19 PM PDT'. The zone is shown because 1:30 AM happens twice on Nov 1.
    Given today, a time in another year says so: 'Jul 26, 2024, 10:00 AM PDT'."""
    local = instant.astimezone(LA)
    year = "" if today is None or local.year == today.year else f", {local.year}"
    return f"{local:%b} {local.day}{year}, {_clock(local.time())} {local:%Z}"


def event_date(day: date | None, today: date) -> str:
    """'Tue Oct 6'; 'Sun Jan 3, 2027' outside this year, so a date never reads as the wrong one."""
    if day is None:
        return "TBA"
    text = f"{day:%a %b} {day.day}"
    return text if day.year == today.year else f"{text}, {day.year}"


def onsale(instant: datetime | None, today: date) -> str:
    return MISSING if instant is None else la_time(instant, today)


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


def run_status_word(status: str, checks_failed: int | None = None, error: str | None = None) -> str:
    """The fetch status, plus what it doesn't say: failed checks, or a run nobody finished."""
    if status == "succeeded" and checks_failed:
        return "succeeded · CHECKS FAILED"
    if status == "failed" and error and error.startswith(ABANDONED):
        return "FAILED (abandoned)"
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
    mb = size / 1024 ** 2
    return "<0.1 MB" if mb < 0.1 else f"{mb:,.1f} MB"  # "0.0 MB" would read as empty


_CHECK_TITLES = {
    "fetched_vs_reported": "Every page fully fetched",
    "unique_vs_total": "Nothing missing vs the API's total",
    "volume_vs_baseline": "Volume normal vs recent runs",
    "window_over_cap": "No window over the API cap",
    "events_without_venue": "Every event has a venue",
    "venues_outside_ca": "Venues outside California",
    "implausible_onsales": "Onsale dates plausible",
}


def check_title(name: str) -> str:
    return _CHECK_TITLES.get(name, name)


def if_it_fails(severity: str) -> str:
    # An error makes ingest exit non-zero, so the scheduled run fails; a warning only shows here.
    return "Run fails" if severity == "error" else "Warning only"


PROBLEM_STATUSES = frozenset({"cancelled", "postponed", "rescheduled"})


def is_problem_status(status: str | None) -> bool:
    return status in PROBLEM_STATUSES


def event_status(status: str | None) -> str:
    if status is None:
        return MISSING
    return status.upper() if is_problem_status(status) else status


# Real names carry these ("Nice as F**k", "[THE X : NEXUS]", "Loose Bricks | ..."). In st.table
# every cell is Markdown, and Streamlit also reads $...$ as maths and :word[...] as a directive.
_MARKDOWN = frozenset("\\`*_{}[]<>()#+-.!|~$:&")


def md_escape(text: str) -> str:
    """Backslash-escape Markdown punctuation so text from the API renders exactly as written."""
    return "".join(f"\\{c}" if c in _MARKDOWN else c for c in text)


def weeks_spanned(first_day: date, last_day: date) -> int:
    """How many Monday-first weeks the days first_day..last_day touch."""
    return (_monday(last_day) - _monday(first_day)).days // 7 + 1


def partial_week(week: date, first_day: date, last_day: date) -> bool:
    """True when first_day..last_day covers only some of the days of the week starting `week`."""
    return week < first_day or week + timedelta(days=6) > last_day


def week_label(week: date, first_day: date, last_day: date) -> str:
    """'Oct 5', or 'Oct 5 (partial)', so a short bar is never mistaken for a quiet week."""
    partial = partial_week(week, first_day, last_day)
    return f"{week:%b} {week.day}" + (" (partial)" if partial else "")


def weekly_caption(first_day: date, last_day: date) -> str:
    first = first_day.weekday() != 0  # the range starts after that week's Monday
    last = last_day.weekday() != 6  # ...or ends before that week's Sunday
    ending = {(True, True): ", and the first and last weeks are partial.",
              (True, False): ", and the first week is partial.",
              (False, True): ", and the last week is partial."}.get((first, last), ".")
    return "Later weeks are naturally lower: shows further out are announced later" + ending


def entered_caption(dates: list[date], today: date) -> str:
    """The shows that appeared only because the 90-day window moved forward, as one line."""
    if not dates:
        return "Entered the 90-day window: none in the last 24 hours."
    first, last = event_date(min(dates), today), event_date(max(dates), today)
    span = first if first == last else f"{first} to {last}"
    return (f"Entered the 90-day window: {_plural(len(dates), 'show')} ({span}), in range only "
            "because the window moved forward.")


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())
