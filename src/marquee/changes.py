"""Change detection: what a desk cares about when a known event comes back different. Pure.

Compared on transformed values, so an implausible onsale that became NULL can't flap. A first
sighting is not a change (new shows come from events.first_seen_run, and entered_window tells a
newly listed show from one the moving 90-day window has only just reached).
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, time

from marquee.transform import EventRow
from marquee.windows import LA

TRACKED = ("status", "local_date", "local_time", "venue", "public_sale_start")


@dataclass(frozen=True)
class Snapshot:
    """The tracked fields of one event, as stored or as just transformed."""

    status: str | None
    local_date: date | None
    local_time: time | None
    venue: str | None  # the venue's Ticketmaster id: stable and readable in the change log
    public_sale_start: datetime | None

    @classmethod
    def of(cls, row: EventRow) -> Snapshot:
        return cls(row.status, row.local_date, row.local_time, row.venue_source_id,
                   row.public_sale_start)


@dataclass(frozen=True)
class Change:
    field: str
    old: str | None
    new: str | None


def detect_changes(stored: Snapshot | None, new: Snapshot) -> list[Change]:
    if stored is None:
        return []
    changes = []
    for f in fields(Snapshot):
        old_text, new_text = _text(getattr(stored, f.name)), _text(getattr(new, f.name))
        if old_text != new_text:
            changes.append(Change(f.name, old_text, new_text))
    return changes


def fold(existing: Change | None, again: Change) -> Change | None:
    """Combine a second change to the same field within one run into the first.

    The run's row keeps the value from before the run and takes the latest new value. If the
    field ended up back where it started, there's no net change: None.
    """
    if existing is None:
        return again
    if existing.field != again.field:
        raise ValueError(f"can't fold a {again.field} change into a {existing.field} change")
    if existing.old == again.new:
        return None
    return Change(existing.field, existing.old, again.new)


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date | time):
        return value.isoformat()
    return str(value)


def entered_window(starts_at: datetime | None, local_date: date | None,
                   previous_end: datetime | None) -> bool:
    """True when a show seen for the first time lay beyond the previous complete run's range: it
    appeared because the window moved forward, not because it was just listed. Undated shows,
    and shows with no earlier range to compare with, count as newly listed."""
    if previous_end is None:
        return False
    if starts_at is not None:
        return starts_at > previous_end  # the API includes the range's end
    if local_date is not None:  # no specific time: previous_end is the first LA day outside
        return local_date >= previous_end.astimezone(LA).date()
    return False
