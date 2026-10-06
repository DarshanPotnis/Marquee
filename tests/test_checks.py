"""Check rules (PLAN §5). Pure: they read a snapshot of the run's facts, never the network or DB."""

from dataclasses import replace

import pytest

from marquee.checks import CHECKS, RunFacts, WindowFacts, run_checks, tolerance

GOOD = RunFacts(
    windows=(WindowFacts("2026-10-06", 156, 156), WindowFacts("2026-10-13", 165, 165)),
    unique_window_ids=1282,
    total_reported=1282,
    unique_events=1301,
    prior_unique=(1300, 1303, 1304),
    events_without_venue=0,
    venues_outside_ca=(),
    onsale_nulled={"placeholder_1900": 239},
)


def result(facts: RunFacts, name: str):  # type: ignore[no-untyped-def]
    return next(r for r in run_checks(facts) if r.name == name)


def test_every_check_runs_once_with_its_severity() -> None:
    assert [(r.name, r.severity) for r in run_checks(GOOD)] == [
        ("fetched_vs_reported", "error"),
        ("unique_vs_total", "error"),
        ("window_over_cap", "error"),
        ("volume_vs_baseline", "error"),
        ("events_without_venue", "warning"),
        ("venues_outside_ca", "warning"),
        ("implausible_onsales", "warning"),
    ]
    assert [name for name, _ in CHECKS] == [r.name for r in run_checks(GOOD)]
    assert all(r.passed for r in run_checks(GOOD))


@pytest.mark.parametrize(("reported", "allowed"), [(0, 2), (100, 2), (299, 2), (300, 3),
                                                   (1282, 12), (5000, 50)])
def test_tolerance_is_the_larger_of_2_and_1_percent(reported: int, allowed: int) -> None:
    assert tolerance(reported) == allowed


# --- fetched_vs_reported --------------------------------------------------------------------------

def test_fetched_vs_reported_passes_within_tolerance() -> None:
    facts = replace(GOOD, windows=(WindowFacts("2026-10-06", 156, 154),))  # gap 2, tolerance 2
    r = result(facts, "fetched_vs_reported")
    assert r.passed and r.observed == 0


def test_fetched_vs_reported_fails_outside_tolerance_and_names_the_window() -> None:
    facts = replace(GOOD, windows=(WindowFacts("2026-10-06", 156, 156),
                                   WindowFacts("2026-10-13", 165, 160)))  # gap 5 > 2
    r = result(facts, "fetched_vs_reported")
    assert not r.passed and r.observed == 1 and r.expected == 0
    assert "2026-10-13" in r.detail and "160 of 165" in r.detail


# --- unique_vs_total ------------------------------------------------------------------------------

def test_unique_vs_total_tolerates_hourly_drift_and_records_the_exact_difference() -> None:
    r = result(replace(GOOD, unique_window_ids=1276, total_reported=1282), "unique_vs_total")
    assert r.passed  # 6 apart, tolerance 12
    assert (r.observed, r.expected) == (1276, 1282)
    assert "difference -6" in r.detail and "tolerance 12" in r.detail


def test_unique_vs_total_fails_beyond_tolerance() -> None:
    r = result(replace(GOOD, unique_window_ids=1200, total_reported=1282), "unique_vs_total")
    assert not r.passed
    assert "difference -82" in r.detail


def test_unique_vs_total_fails_when_the_total_could_not_be_read() -> None:
    r = result(replace(GOOD, total_reported=None), "unique_vs_total")
    assert not r.passed and "not read" in r.detail


# --- window_over_cap ------------------------------------------------------------------------------

def test_window_over_cap_passes_at_1000() -> None:
    assert result(replace(GOOD, windows=(WindowFacts("2026-11-01", 1000, 1000),)),
                  "window_over_cap").passed


def test_window_over_cap_fails_above_1000_and_names_the_day() -> None:
    r = result(replace(GOOD, windows=(WindowFacts("2026-11-01", 1050, 1050),)), "window_over_cap")
    assert not r.passed and r.observed == 1 and "2026-11-01 (1050)" in r.detail


# --- volume_vs_baseline ---------------------------------------------------------------------------

def test_volume_vs_baseline_is_skipped_until_3_prior_runs_exist() -> None:
    r = result(replace(GOOD, unique_events=10, prior_unique=(1300, 1300)), "volume_vs_baseline")
    assert r.passed and "skipped" in r.detail and "2 prior" in r.detail


def test_volume_vs_baseline_passes_at_70_percent_of_the_median() -> None:
    r = result(replace(GOOD, unique_events=910, prior_unique=(1300, 1200, 1400)),
               "volume_vs_baseline")
    assert r.passed and r.expected == 910  # 70% of the median, 1300


def test_volume_vs_baseline_fails_below_70_percent_of_the_median() -> None:
    r = result(replace(GOOD, unique_events=909, prior_unique=(1300, 1200, 1400)),
               "volume_vs_baseline")
    assert not r.passed and r.observed == 909


def test_volume_vs_baseline_uses_at_most_the_last_7_runs() -> None:
    prior = (1300,) * 7 + (10_000,) * 5  # newest first; the old huge runs are ignored
    assert result(replace(GOOD, unique_events=1000, prior_unique=prior),
                  "volume_vs_baseline").passed


# --- the warnings ---------------------------------------------------------------------------------

def test_events_without_venue_warns_when_any_are_missing() -> None:
    assert result(GOOD, "events_without_venue").passed
    r = result(replace(GOOD, events_without_venue=3), "events_without_venue")
    assert not r.passed and r.severity == "warning" and r.observed == 3


def test_venues_outside_ca_warns_and_lists_them() -> None:
    assert result(GOOD, "venues_outside_ca").passed
    far = ("Studio Theatre (Perth, ON)", "The Olaus Ice Palace (Rossland, BC)")
    r = result(replace(GOOD, venues_outside_ca=far), "venues_outside_ca")
    assert not r.passed and r.observed == 2 and "Perth, ON" in r.detail


def test_implausible_onsales_accepts_the_known_placeholder() -> None:
    r = result(GOOD, "implausible_onsales")
    assert r.passed and r.observed == 0 and "placeholder_1900 239" in r.detail


def test_implausible_onsales_warns_on_any_other_reason() -> None:
    r = result(replace(GOOD, onsale_nulled={"placeholder_1900": 239, "too_early": 1}),
               "implausible_onsales")
    assert not r.passed and r.severity == "warning" and r.observed == 1
    assert "too_early 1" in r.detail
