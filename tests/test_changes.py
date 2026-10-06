"""Change detection between the stored event and the freshly transformed one. Pure: no I/O."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

import pytest

from marquee.changes import TRACKED, Change, Snapshot, detect_changes, fold
from marquee.transform import EventRow

STORED = Snapshot(status="onsale", local_date=date(2026, 11, 1), local_time=time(20, 0),
                  venue="KovSYNTH001", public_sale_start=datetime(2026, 8, 1, 17, tzinfo=UTC))


def test_a_first_sighting_is_not_a_change() -> None:
    assert detect_changes(None, STORED) == []


def test_an_unchanged_rerun_produces_no_changes() -> None:
    assert detect_changes(STORED, replace(STORED)) == []


@pytest.mark.parametrize(
    ("field", "new_value", "old_text", "new_text"),
    [
        ("status", "cancelled", "onsale", "cancelled"),
        ("local_date", date(2026, 11, 8), "2026-11-01", "2026-11-08"),
        ("local_time", time(19, 30), "20:00:00", "19:30:00"),
        ("venue", "KovSYNTH002", "KovSYNTH001", "KovSYNTH002"),
        ("public_sale_start", datetime(2026, 8, 2, 17, tzinfo=UTC),
         "2026-08-01T17:00:00Z", "2026-08-02T17:00:00Z"),
    ],
)
def test_each_tracked_field_that_differs_is_one_change(
    field: str, new_value: object, old_text: str, new_text: str
) -> None:
    assert detect_changes(STORED, replace(STORED, **{field: new_value})) == \
        [Change(field, old_text, new_text)]


def test_an_undated_event_getting_a_date_is_a_change() -> None:
    undated = replace(STORED, status="postponed", local_date=None, local_time=None)
    dated = replace(STORED, status="rescheduled", local_date=date(2026, 12, 5),
                    local_time=time(20, 0))
    assert detect_changes(undated, dated) == [
        Change("status", "postponed", "rescheduled"),
        Change("local_date", None, "2026-12-05"),
        Change("local_time", None, "20:00:00"),
    ]


def test_a_dated_event_going_tba_is_a_change() -> None:
    tba = replace(STORED, status="postponed", local_date=None, local_time=None)
    assert detect_changes(STORED, tba) == [
        Change("status", "onsale", "postponed"),
        Change("local_date", "2026-11-01", None),
        Change("local_time", "20:00:00", None),
    ]


def test_null_to_null_is_not_a_change() -> None:
    # e.g. one placeholder onsale replaced by another: both are stored as NULL.
    no_onsale = replace(STORED, public_sale_start=None)
    assert detect_changes(no_onsale, replace(no_onsale)) == []


def test_changes_come_out_in_tracked_field_order() -> None:
    moved = replace(STORED, venue="KovSYNTH002", status="rescheduled", local_date=date(2026, 12, 1))
    assert [c.field for c in detect_changes(STORED, moved)] == ["status", "local_date", "venue"]
    assert TRACKED == ("status", "local_date", "local_time", "venue", "public_sale_start")


def test_a_snapshot_is_taken_from_a_transformed_event() -> None:
    row = EventRow(source_id="vv1", name="Show", venue_source_id="KovSYNTH001",
                   local_date=date(2026, 11, 1), local_time=time(20, 0),
                   starts_at=datetime(2026, 11, 2, 4, tzinfo=UTC), status="onsale",
                   segment="Music", genre="Rock",
                   public_sale_start=datetime(2026, 8, 1, 17, tzinfo=UTC), url=None,
                   attraction_ids=())
    assert Snapshot.of(row) == STORED


# --- The same field changing twice in one run -----------------------------------------------------
# An event can be seen on a probe page and again in a child window seconds later. If it changed in
# between, the run already holds a change row for that field: fold the new one into it.

def test_fold_into_nothing_is_the_change_itself() -> None:
    assert fold(None, Change("status", "onsale", "postponed")) == \
        Change("status", "onsale", "postponed")


def test_a_repeat_change_keeps_the_original_old_value_and_takes_the_new_one() -> None:
    first = Change("status", "onsale", "postponed")
    again = Change("status", "postponed", "cancelled")
    assert fold(first, again) == Change("status", "onsale", "cancelled")


def test_a_repeat_change_back_to_the_start_leaves_no_change() -> None:
    first = Change("status", "onsale", "postponed")
    back = Change("status", "postponed", "onsale")
    assert fold(first, back) is None


def test_fold_refuses_changes_to_different_fields() -> None:
    with pytest.raises(ValueError):
        fold(Change("status", "a", "b"), Change("venue", "b", "c"))


# --- New shows: newly listed, or only just inside the moving 90-day window ------------------------

# The previous complete run's range ended at LA midnight starting Mon Jan 4, 2027 (08:00 UTC).
PREVIOUS_END = datetime(2027, 1, 4, 8, tzinfo=UTC)


@pytest.mark.parametrize(
    ("starts_at", "local_date", "entered"),
    [
        (datetime(2027, 1, 4, 5, tzinfo=UTC), date(2027, 1, 3), False),  # Sun 9 PM PT: was inside
        (PREVIOUS_END, date(2027, 1, 4), False),  # exactly on the edge: the API includes edges
        (PREVIOUS_END + timedelta(seconds=1), date(2027, 1, 4), True),
        (datetime(2027, 1, 5, 4, tzinfo=UTC), date(2027, 1, 4), True),  # Mon 8 PM PT
        (None, date(2027, 1, 3), False),  # no specific time: judged by its date
        (None, date(2027, 1, 4), True),
        (None, None, False),  # undated (TBA/TBD): always newly listed
    ],
)
def test_a_show_entered_the_window_only_if_it_lay_beyond_the_previous_range(
    starts_at: datetime | None, local_date: date | None, entered: bool
) -> None:
    from marquee.changes import entered_window
    assert entered_window(starts_at, local_date, PREVIOUS_END) is entered


def test_without_a_previous_range_a_show_counts_as_newly_listed() -> None:
    from marquee.changes import entered_window
    assert entered_window(datetime(2027, 1, 5, 4, tzinfo=UTC), date(2027, 1, 4), None) is False
