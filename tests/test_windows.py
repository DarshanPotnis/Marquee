"""Window planning in Los Angeles calendar days, sent to the API as UTC. Pure: no I/O."""

from datetime import UTC, date, datetime, timedelta

import pytest

from marquee.windows import (
    LA,
    RANGE_DAYS,
    Window,
    api_time,
    local_midnight,
    plan_range,
    plan_windows,
    split,
)

NOW = datetime(2026, 10, 6, 15, 22, 49, tzinfo=UTC)  # Tue 2026-10-06 08:22:49 PDT


def hours(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 3600


def assert_contiguous(windows: list[Window], start: datetime, end: datetime) -> None:
    assert windows[0].start == start
    assert windows[-1].end == end
    for a, b in zip(windows, windows[1:], strict=False):
        # Shared edge: the API includes both ends (api-notes), so no gap and nothing skipped.
        assert a.end == b.start


def leaves(window: Window) -> list[Window]:
    halves = split(window)
    return [window] if halves is None else leaves(halves[0]) + leaves(halves[1])


def test_the_range_runs_from_now_to_the_local_midnight_90_days_out() -> None:
    r = plan_range(NOW)
    assert r.start == NOW
    assert r.end == datetime(2027, 1, 4, 8, 0, tzinfo=UTC)  # 2027-01-04 00:00 PST
    assert r.first_day == date(2026, 10, 6)
    assert r.days == RANGE_DAYS == 90


def test_microseconds_in_now_are_dropped() -> None:
    assert plan_range(NOW.replace(microsecond=500_000)).start == NOW


def test_weekly_windows_cover_the_range_with_no_gaps_or_overlaps() -> None:
    windows = plan_windows(NOW)
    assert len(windows) == 13
    assert_contiguous(windows, NOW, plan_range(NOW).end)
    assert [w.days for w in windows] == [7] * 12 + [6]


def test_every_boundary_after_the_first_is_a_los_angeles_midnight() -> None:
    windows = plan_windows(NOW)
    for instant in [w.start for w in windows[1:]] + [windows[-1].end]:
        local = instant.astimezone(LA)
        assert (local.hour, local.minute, local.second) == (0, 0, 0)


def test_fall_back_day_is_25_hours_and_its_neighbours_24() -> None:
    def length(d: date) -> float:
        return hours(local_midnight(d), local_midnight(d + timedelta(days=1)))

    assert length(date(2026, 10, 31)) == 24
    assert length(date(2026, 11, 1)) == 25
    assert length(date(2026, 11, 2)) == 24
    assert local_midnight(date(2026, 11, 1)) == datetime(2026, 11, 1, 7, tzinfo=UTC)  # PDT
    assert local_midnight(date(2026, 11, 2)) == datetime(2026, 11, 2, 8, tzinfo=UTC)  # PST


def test_spring_forward_day_is_23_hours() -> None:
    def length(d: date) -> float:
        return hours(local_midnight(d), local_midnight(d + timedelta(days=1)))

    assert (length(date(2027, 3, 13)), length(date(2027, 3, 14)), length(date(2027, 3, 15))) == \
        (24, 23, 24)


def test_the_window_holding_fall_back_is_an_hour_longer_and_still_contiguous() -> None:
    windows = plan_windows(NOW)
    fall_back = local_midnight(date(2026, 11, 1))
    holder = next(w for w in windows if w.start < fall_back < w.end)
    assert hours(holder.start, holder.end) == 7 * 24 + 1


def test_a_plan_across_spring_forward_has_a_short_week_and_no_gaps() -> None:
    later = datetime(2027, 1, 20, 18, 0, tzinfo=UTC)  # the range now includes 2027-03-14
    windows = plan_windows(later)
    assert_contiguous(windows, later, plan_range(later).end)
    spring = local_midnight(date(2027, 3, 14))
    holder = next(w for w in windows if w.start < spring < w.end)
    assert hours(holder.start, holder.end) == 7 * 24 - 1


def test_split_halves_at_a_local_midnight() -> None:
    week = plan_windows(NOW)[3]  # Tue Oct 27 to Tue Nov 3, which holds the 25-hour day
    halves = split(week)
    assert halves is not None
    left, right = halves
    assert (left.days, right.days) == (3, 4)
    assert (left.start, left.end, right.start, right.end) == \
        (week.start, local_midnight(date(2026, 10, 30)), local_midnight(date(2026, 10, 30)),
         week.end)
    assert right.first_day == date(2026, 10, 30)
    assert hours(right.start, right.end) == 4 * 24 + 1


def test_splitting_the_partial_first_window_keeps_now_as_its_start() -> None:
    halves = split(plan_windows(NOW)[0])
    assert halves is not None
    assert halves[0].start == NOW
    assert halves[0].end == local_midnight(date(2026, 10, 9))


def test_a_one_day_window_cannot_be_split() -> None:
    day = Window(local_midnight(date(2026, 11, 1)), local_midnight(date(2026, 11, 2)),
                 date(2026, 11, 1), 1)
    assert split(day) is None


def test_splitting_all_the_way_down_rebuilds_the_range_exactly() -> None:
    days = [leaf for w in plan_windows(NOW) for leaf in leaves(w)]
    assert len(days) == 90
    assert all(d.days == 1 for d in days)
    assert_contiguous(days, NOW, plan_range(NOW).end)
    nov_1 = next(d for d in days if d.first_day == date(2026, 11, 1))
    assert hours(nov_1.start, nov_1.end) == 25


def test_api_time_is_the_only_format_the_api_accepts() -> None:
    assert api_time(datetime(2026, 11, 1, 7, 0, tzinfo=UTC)) == "2026-11-01T07:00:00Z"
    assert api_time(datetime(2026, 11, 1, 0, 0, tzinfo=LA)) == "2026-11-01T07:00:00Z"
    assert api_time(datetime(2026, 11, 1, 7, 0, 0, 999_999, tzinfo=UTC)) == "2026-11-01T07:00:00Z"


def test_naive_datetimes_are_refused() -> None:
    with pytest.raises(ValueError, match="timezone"):
        api_time(datetime(2026, 11, 1))
    with pytest.raises(ValueError, match="timezone"):
        plan_range(datetime(2026, 10, 6, 15, 0))


def test_a_window_must_run_forwards() -> None:
    a, b = local_midnight(date(2026, 11, 1)), local_midnight(date(2026, 11, 2))
    with pytest.raises(ValueError):
        Window(b, a, date(2026, 11, 1), 1)
