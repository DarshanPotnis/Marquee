"""One raw page body -> clean rows. Pure: no network, no database, no clock (run time is passed in).

The rules come from what the API actually sends (docs/api-notes.md §9, §13, §14): coordinates
arrive as strings, city names can carry stray spaces, "Undefined" is a placeholder rather than a
genre, and the public onsale date is sometimes a placeholder (1900-01-01) or otherwise implausible.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ONSALE_MAX_LEAD = timedelta(days=730)  # a public onsale more than 2 years out isn't plausible
PLACEHOLDERS = frozenset({"Undefined"})  # used instead of a classification, not as one
DEFAULT_TZ = ZoneInfo("America/Los_Angeles")
API_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class VenueRow:
    source_id: str
    name: str
    city: str | None
    state: str | None
    timezone: str | None
    latitude: float | None
    longitude: float | None


@dataclass(frozen=True)
class AttractionRow:
    source_id: str
    name: str
    segment: str | None
    genre: str | None


@dataclass(frozen=True)
class EventRow:
    source_id: str
    name: str
    venue_source_id: str | None
    local_date: date | None
    local_time: time | None
    starts_at: datetime | None  # None when the date or the time isn't known
    status: str | None  # as the API spells it: onsale | offsale | cancelled | postponed | ...
    segment: str | None
    genre: str | None
    public_sale_start: datetime | None  # None when missing or implausible
    url: str | None
    attraction_ids: tuple[str, ...]  # in listed order; position 0 is the headliner


@dataclass(frozen=True)
class PageRows:
    events: tuple[EventRow, ...]
    venues: tuple[VenueRow, ...]
    attractions: tuple[AttractionRow, ...]
    onsale_nulled: dict[str, int] = field(default_factory=dict)  # implausible onsales, by reason
    skipped_events: int = 0  # events without an id or a name


def transform_page(body: Mapping[str, Any], *, run_at: datetime) -> PageRows:
    venues: dict[str, VenueRow] = {}
    attractions: dict[str, AttractionRow] = {}
    events: list[EventRow] = []
    nulled: Counter[str] = Counter()
    skipped = 0
    for raw in _list(_dict(body.get("_embedded")).get("events")):
        if not isinstance(raw, dict) or not _text(raw.get("id")) or not _text(raw.get("name")):
            skipped += 1
            continue
        venue = _venue(_first(_dict(raw.get("_embedded")).get("venues")))
        if venue is not None:
            venues[venue.source_id] = venue
        listed = [a for a in map(_attraction, _list(_dict(raw.get("_embedded")).get("attractions")))
                  if a is not None]
        for a in listed:
            attractions.setdefault(a.source_id, a)
        event, reason = _event(raw, venue, listed, run_at)
        if reason is not None:
            nulled[reason] += 1
        events.append(event)
    return PageRows(tuple(events), tuple(venues.values()), tuple(attractions.values()),
                    dict(nulled), skipped)


def _event(
    raw: dict[str, Any], venue: VenueRow | None, listed: list[AttractionRow], run_at: datetime
) -> tuple[EventRow, str | None]:
    dates = _dict(raw.get("dates"))
    start = _dict(dates.get("start"))
    local_date = _parse(date.fromisoformat, start.get("localDate"))
    local_time = _parse(time.fromisoformat, start.get("localTime"))
    starts_at = _parse(_utc, start.get("dateTime"))
    segment, genre = _classification(raw.get("classifications"))
    tz = _zone(_text(dates.get("timezone")) or (venue.timezone if venue else None))
    # What the onsale must come before: the start; else the end of the local day; else, for an
    # undated (TBA) event, its original date if the API still gives one.
    original = _dict(dates.get("initialStartDate"))
    reference = (starts_at or _end_of_day(local_date, tz)
                 or _parse(_utc, original.get("dateTime"))
                 or _end_of_day(_parse(date.fromisoformat, original.get("localDate")), tz))
    onsale, reason = _plausible_onsale(
        _dict(_dict(raw.get("sales")).get("public")).get("startDateTime"), reference, run_at)
    ids = tuple(dict.fromkeys(a.source_id for a in listed))  # first listing wins
    return EventRow(
        source_id=str(raw["id"]), name=str(raw["name"]).strip(),
        venue_source_id=venue.source_id if venue else None,
        local_date=local_date, local_time=local_time, starts_at=starts_at,
        status=_text(_dict(dates.get("status")).get("code")),
        segment=segment, genre=genre, public_sale_start=onsale, url=_text(raw.get("url")),
        attraction_ids=ids,
    ), reason


def _plausible_onsale(
    value: object, event_start: datetime | None, run_at: datetime
) -> tuple[datetime | None, str | None]:
    """Keep an onsale only if it's plausible; otherwise None plus a reason to count."""
    if value is None:
        return None, None
    onsale = _parse(_utc, value)
    if onsale is None:
        return None, "unparseable"
    if onsale.year == 1900:
        return None, "placeholder_1900"
    if event_start is not None:
        if onsale > event_start:
            return None, "after_event"
        if onsale < event_start - ONSALE_MAX_LEAD:
            return None, "too_early"
        return onsale, None
    # Undated, with no original date: a postponed show's onsale can be years old and still
    # real, so accept any past onsale. Only one more than 2 years in the future is implausible.
    if onsale > run_at + ONSALE_MAX_LEAD:
        return None, "too_late"
    return onsale, None


def _end_of_day(day: date | None, tz: ZoneInfo) -> datetime | None:
    # A date-only event counts as starting at the end of its local day.
    if day is None:
        return None
    return datetime.combine(day + timedelta(days=1), time(), tzinfo=tz).astimezone(UTC)


def _venue(raw: object) -> VenueRow | None:
    if not isinstance(raw, dict) or not _text(raw.get("id")) or not _text(raw.get("name")):
        return None
    location = _dict(raw.get("location"))
    return VenueRow(
        source_id=str(raw["id"]), name=str(raw["name"]).strip(),
        city=_text(_dict(raw.get("city")).get("name")),
        state=_text(_dict(raw.get("state")).get("stateCode")),
        timezone=_text(raw.get("timezone")),
        latitude=_parse(float, location.get("latitude")),
        longitude=_parse(float, location.get("longitude")),
    )


def _attraction(raw: object) -> AttractionRow | None:
    if not isinstance(raw, dict) or not _text(raw.get("id")) or not _text(raw.get("name")):
        return None
    segment, genre = _classification(raw.get("classifications"))
    return AttractionRow(str(raw["id"]), str(raw["name"]).strip(), segment, genre)


def _classification(raw: object) -> tuple[str | None, str | None]:
    options = [c for c in _list(raw) if isinstance(c, dict)]
    chosen = next((c for c in options if c.get("primary") is True), options[0] if options else {})

    def name(key: str) -> str | None:
        value = _text(_dict(chosen.get(key)).get("name"))
        return None if value in PLACEHOLDERS else value

    return name("segment"), name("genre")


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name) if name else DEFAULT_TZ
    except (ZoneInfoNotFoundError, ValueError):
        return DEFAULT_TZ


def _utc(value: str) -> datetime:
    return datetime.strptime(value, API_TIME_FORMAT).replace(tzinfo=UTC)


def _parse[T](parser: Callable[[str], T], value: object) -> T | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parser(value.strip())
    except ValueError:
        return None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _first(value: object) -> object:
    items = _list(value)
    return items[0] if items else None
