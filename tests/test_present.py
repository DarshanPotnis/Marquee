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


# --- Dashboard review: words, dates, weeks, sizes -------------------------------------------------

TODAY = date(2026, 10, 6)  # a Tuesday


def test_every_check_has_a_plain_english_title() -> None:
    from marquee.checks import CHECKS
    from marquee.present import check_title
    assert {name: check_title(name) for name, _ in CHECKS} == {
        "fetched_vs_reported": "Every page fully fetched",
        "unique_vs_total": "Nothing missing vs the API's total",
        "volume_vs_baseline": "Volume normal vs recent runs",
        "window_over_cap": "No window over the API cap",
        "events_without_venue": "Every event has a venue",
        "venues_outside_ca": "Venues outside California",
        "implausible_onsales": "Onsale dates plausible",
    }
    assert check_title("a_new_check") == "a_new_check"  # never blank


def test_what_happens_if_a_check_fails() -> None:
    from marquee.present import if_it_fails
    assert (if_it_fails("error"), if_it_fails("warning")) == ("Run fails", "Warning only")


@pytest.mark.parametrize(
    ("day", "text"),
    [(date(2026, 10, 6), "Tue Oct 6"), (date(2026, 12, 31), "Thu Dec 31"),
     (date(2027, 1, 3), "Sun Jan 3, 2027"), (date(2025, 11, 14), "Fri Nov 14, 2025"),
     (None, "TBA")],
)
def test_event_dates_carry_the_weekday_and_a_year_outside_this_one(day: date | None,
                                                                   text: str) -> None:
    from marquee.present import event_date
    assert event_date(day, TODAY) == text


def test_times_carry_a_year_outside_this_one_when_asked() -> None:
    assert la_time(datetime(2024, 7, 26, 17, tzinfo=UTC), TODAY) == "Jul 26, 2024, 10:00 AM PDT"
    assert la_time(datetime(2026, 8, 1, 17, tzinfo=UTC), TODAY) == "Aug 1, 10:00 AM PDT"
    # New Year's Eve in LA is already next year in UTC: the year follows LA.
    assert la_time(datetime(2027, 1, 1, 7, tzinfo=UTC), TODAY) == "Dec 31, 11:00 PM PST"
    assert la_time(datetime(2024, 7, 26, 17, tzinfo=UTC)) == "Jul 26, 10:00 AM PDT"


def test_a_missing_onsale_is_a_dash() -> None:
    from marquee.present import MISSING, onsale
    assert MISSING == "—"
    assert onsale(None, TODAY) == "—"
    assert onsale(datetime(2025, 3, 1, 18, tzinfo=UTC), TODAY) == "Mar 1, 2025, 10:00 AM PST"


@pytest.mark.parametrize(
    ("status", "word", "problem"),
    [("cancelled", "CANCELLED", True), ("postponed", "POSTPONED", True),
     ("rescheduled", "RESCHEDULED", True), ("onsale", "onsale", False),
     ("offsale", "offsale", False), (None, "—", False)],
)
def test_problem_statuses_are_shouted(status: str | None, word: str, problem: bool) -> None:
    from marquee.present import event_status, is_problem_status
    assert (event_status(status), is_problem_status(status)) == (word, problem)


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [("Synthetic Hall", "Synthetic Hall"),
     ("Nice as F**k", r"Nice as F\*\*k"),
     ("Creed Fisher *This Whiskey and Me Tour*", r"Creed Fisher \*This Whiskey and Me Tour\*"),
     ("KCRW Presents: [MANIC STREET PREACHERS]", r"KCRW Presents\: \[MANIC STREET PREACHERS\]"),
     ("$uicideboy$ | Live", r"\$uicideboy\$ \| Live"),
     ("Noche De Exitos #2", r"Noche De Exitos \#2"),
     ("a_b ~c~ `d` <e> \\", r"a\_b \~c\~ \`d\` \<e\> \\")],
)
def test_text_from_the_api_is_escaped_for_markdown(raw: str, escaped: str) -> None:
    from marquee.present import md_escape
    assert md_escape(raw) == escaped


@pytest.mark.parametrize(
    ("first", "last", "n"),
    [(date(2026, 10, 6), date(2027, 1, 3), 13),  # Tue → Sun: both ends inside 13 weeks
     (date(2026, 10, 5), date(2027, 1, 2), 13),  # Mon → Sat
     (date(2026, 10, 7), date(2027, 1, 4), 14),  # Wed → Mon: the last day starts a 14th week
     (date(2026, 10, 11), date(2026, 10, 11), 1)],
)
def test_the_weeks_a_range_touches(first: date, last: date, n: int) -> None:
    from marquee.present import weeks_spanned
    assert weeks_spanned(first, last) == n


def test_a_week_the_range_only_partly_covers_is_marked_partial() -> None:
    from marquee.present import week_label
    first, last = date(2026, 10, 7), date(2027, 1, 4)  # Wed → Mon
    assert week_label(date(2026, 10, 5), first, last) == "Oct 5 (partial)"
    assert week_label(date(2026, 10, 12), first, last) == "Oct 12"
    assert week_label(date(2026, 12, 28), first, last) == "Dec 28"
    assert week_label(date(2027, 1, 4), first, last) == "Jan 4 (partial)"
    # Monday to Sunday: no partial week at either end.
    assert week_label(date(2026, 10, 5), date(2026, 10, 5), date(2026, 10, 18)) == "Oct 5"
    assert week_label(date(2026, 10, 12), date(2026, 10, 5), date(2026, 10, 18)) == "Oct 12"


@pytest.mark.parametrize(
    ("first", "last", "ending"),
    [(date(2026, 10, 5), date(2027, 1, 2), ", and the last week is partial."),
     (date(2026, 10, 6), date(2027, 1, 3), ", and the first week is partial."),
     (date(2026, 10, 7), date(2027, 1, 4), ", and the first and last weeks are partial."),
     (date(2026, 10, 5), date(2026, 10, 18), ".")],
)
def test_the_weekly_caption_names_only_the_weeks_that_are_partial(first: date, last: date,
                                                                  ending: str) -> None:
    from marquee.present import weekly_caption
    assert weekly_caption(first, last) == ("Later weeks are naturally lower: shows further out "
                                           "are announced later" + ending)


@pytest.mark.parametrize(
    ("size", "text"),
    [(0, "<0.1 MB"), (8192, "<0.1 MB"), (104_857, "<0.1 MB"), (104_858, "0.1 MB"),
     (13_480_000, "12.9 MB")],
)
def test_sizes_under_a_tenth_of_a_megabyte(size: int, text: str) -> None:
    from marquee.present import megabytes
    assert megabytes(size) == text
