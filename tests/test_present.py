"""Presentation helpers for the dashboard. Pure: plain values in, display strings out."""

from datetime import UTC, date, datetime, time, timedelta

import pytest

from marquee.present import (
    SCOPE_LABEL,
    ago,
    change_text,
    completeness_sentence,
    freshness,
    la_date,
    la_time,
    number,
    result_word,
    run_status_word,
)

NOW = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)


def test_the_scope_label_is_exact() -> None:
    assert SCOPE_LABEL == ("Ticketmaster LA market (DMA 324) · Music · next 90 days · "
                           "face-value data, no resale prices")


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "just now"), (59, "just now"), (60, "1 minute ago"), (59 * 60, "59 minutes ago"),
     (3600, "1 hour ago"), (47 * 3600, "47 hours ago"), (48 * 3600, "2 days ago")],
)
def test_ago(seconds: int, text: str) -> None:
    assert ago(NOW - timedelta(seconds=seconds), NOW) == text


def test_times_are_shown_in_la_with_the_zone_either_side_of_the_clock_change() -> None:
    assert la_time(datetime(2026, 10, 6, 20, 19, tzinfo=UTC)) == "Oct 6, 1:19 PM PDT"
    assert la_time(datetime(2026, 11, 1, 8, 30, tzinfo=UTC)) == "Nov 1, 1:30 AM PDT"
    assert la_time(datetime(2026, 11, 1, 9, 30, tzinfo=UTC)) == "Nov 1, 1:30 AM PST"


def test_dates_are_short() -> None:
    assert la_date(date(2026, 11, 14)) == "Nov 14"
    assert la_date(None) == "TBA"


@pytest.mark.parametrize(
    ("minutes_old", "level", "word"),
    [(0, "fresh", "Fresh"), (89, "fresh", "Fresh"), (90, "late", "LATE"), (179, "late", "LATE"),
     (180, "stale", "STALE"), (2000, "stale", "STALE")],
)
def test_freshness_is_measured_in_schedule_intervals(minutes_old: int, level: str,
                                                     word: str) -> None:
    f = freshness(NOW - timedelta(minutes=minutes_old), NOW, interval_minutes=60)
    assert (f.level, f.word) == (level, word)


def test_freshness_with_no_runs() -> None:
    f = freshness(None, NOW, interval_minutes=60)
    assert (f.level, f.word) == ("none", "NO RUNS YET")


def test_freshness_follows_the_schedule_setting() -> None:
    assert freshness(NOW - timedelta(minutes=200), NOW, interval_minutes=180).level == "fresh"


@pytest.mark.parametrize(
    ("reported", "received", "text"),
    [
        (1289, 1289, "1,289 reported · 1,289 received · 0 missing"),
        (1282, 1200, "1,282 reported · 1,200 received · 82 missing"),
        (1289, 1291, "1,289 reported · 1,291 received · 0 missing (2 more than the total: drift)"),
    ],
)
def test_the_completeness_sentence(reported: int, received: int, text: str) -> None:
    assert completeness_sentence(reported, received) == text


def test_numbers_get_thousands_separators() -> None:
    assert (number(1308), number(0), number(None)) == ("1,308", "0", "-")


@pytest.mark.parametrize(
    ("field", "old", "new", "text"),
    [
        ("status", "onsale", "postponed", "onsale → postponed"),
        ("local_date", "2026-11-14", "2026-12-02", "Nov 14 → Dec 2"),
        ("local_date", None, "2026-12-04", "TBA → Dec 4"),
        ("local_date", "2026-11-14", None, "Nov 14 → TBA"),
        ("local_time", "20:00:00", "19:30:00", "8:00 PM → 7:30 PM"),
        ("venue", "Synthetic Hall", "Other Hall", "Synthetic Hall → Other Hall"),
        ("public_sale_start", "2026-08-01T17:00:00Z", None, "Aug 1, 10:00 AM PDT → none"),
    ],
)
def test_a_change_reads_old_to_new(field: str, old: str | None, new: str | None,
                                   text: str) -> None:
    assert change_text(field, old, new) == text


@pytest.mark.parametrize(
    ("passed", "severity", "word"),
    [(True, "error", "passed"), (True, "warning", "passed"), (False, "error", "FAILED"),
     (False, "warning", "WARNING")],
)
def test_check_results_always_carry_a_word(passed: bool, severity: str, word: str) -> None:
    assert result_word(passed, severity) == word


@pytest.mark.parametrize(
    ("status", "word"),
    [("succeeded", "succeeded"), ("partial", "PARTIAL"), ("failed", "FAILED"),
     ("running", "running")],
)
def test_run_statuses_always_carry_a_word(status: str, word: str) -> None:
    assert run_status_word(status) == word


def test_event_times_show_tba_when_unknown() -> None:
    assert la_date(date(2026, 12, 4)) == "Dec 4"
    assert change_text("local_time", None, time(20, 0).isoformat()) == "TBA → 8:00 PM"


@pytest.mark.parametrize(("failed", "warnings", "text"),
                         [(0, 0, "0 failed · 0 warnings"), (0, 1, "0 failed · 1 warning"),
                          (2, 3, "2 failed · 3 warnings")])
def test_the_checks_summary(failed: int, warnings: int, text: str) -> None:
    from marquee.present import checks_summary
    assert checks_summary(failed, warnings) == text


def test_event_times_and_field_labels_and_sizes() -> None:
    from marquee.present import event_time, field_label, megabytes
    assert (event_time(time(20, 0)), event_time(None)) == ("8:00 PM", "TBA")
    assert [field_label(f) for f in ("status", "local_date", "local_time", "venue",
                                     "public_sale_start")] == \
        ["Status", "Date", "Time", "Venue", "Public onsale"]
    assert (megabytes(13_480_000), megabytes(1024 ** 3)) == ("12.9 MB", "1,024.0 MB")
