"""Check rules (PLAN §5). Pure: they read a snapshot of the run's facts, never the network or DB.

Severity "error" means the run's data can't be trusted as complete; "warning" means something
upstream looks off. An error-level failure makes `ingest` exit non-zero, which turns the scheduled
GitHub Actions run red and notifies its owner (PLAN §5, alert path).
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from fractions import Fraction

from marquee.fetch import CAP

BASELINE_RUNS = 7  # compare with the median of the last 7 succeeded runs
BASELINE_MIN_RUNS = 3  # ...but only once at least 3 exist
BASELINE_FLOOR = Fraction(7, 10)  # below 70% of that median is suspicious; exact, not 0.7
KNOWN_PLACEHOLDERS = frozenset({"placeholder_1900"})  # expected; any other reason is news


@dataclass(frozen=True)
class WindowFacts:
    label: str  # the window's first local day, e.g. "2026-10-06"
    reported: int
    fetched: int


@dataclass(frozen=True)
class RunFacts:
    windows: tuple[WindowFacts, ...]  # final windows only
    unique_window_ids: int  # distinct event ids across the windows (undated calls excluded)
    total_reported: int | None  # the API's total for the whole range, read right after
    unique_events: int  # distinct events loaded this run, undated included
    prior_unique: tuple[int, ...]  # unique_events of earlier succeeded runs, newest first
    events_without_venue: int
    venues_outside_ca: tuple[str, ...]  # "Name (City, ST)"
    onsale_nulled: Mapping[str, int]


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    severity: str  # error | warning
    observed: float | None
    expected: float | None
    detail: str


def tolerance(reported: int) -> int:
    """How far apart two counts may be: totals drift by a few events an hour, so allow the
    larger of 2 events and 1%. Strict equality would turn the scheduled run red for no reason."""
    return max(2, reported // 100)


def fetched_vs_reported(f: RunFacts) -> CheckResult:
    bad = [w for w in f.windows if abs(w.fetched - w.reported) > tolerance(w.reported)]
    gap = max((abs(w.fetched - w.reported) for w in f.windows), default=0)
    detail = (", ".join(f"{w.label}: {w.fetched} of {w.reported}" for w in bad) if bad
              else f"{len(f.windows)} windows within tolerance; largest gap {gap}")
    return CheckResult("fetched_vs_reported", not bad, "error", len(bad), 0, detail)


def unique_vs_total(f: RunFacts) -> CheckResult:
    if f.total_reported is None:
        return CheckResult("unique_vs_total", False, "error", f.unique_window_ids, None,
                           "the whole-range total was not read")
    diff = f.unique_window_ids - f.total_reported
    allowed = tolerance(f.total_reported)
    return CheckResult(
        "unique_vs_total", abs(diff) <= allowed, "error", f.unique_window_ids, f.total_reported,
        f"unique {f.unique_window_ids} vs total {f.total_reported}: difference {diff:+d}"
        f" (tolerance {allowed})".replace("difference +0", "difference 0"),
    )


def window_over_cap(f: RunFacts) -> CheckResult:
    over = [w for w in f.windows if w.reported > CAP]
    detail = (", ".join(f"{w.label} ({w.reported})" for w in over) if over
              else f"no window over {CAP}")
    return CheckResult("window_over_cap", not over, "error", len(over), 0, detail)


def volume_vs_baseline(f: RunFacts) -> CheckResult:
    prior = f.prior_unique[:BASELINE_RUNS]
    if len(prior) < BASELINE_MIN_RUNS:
        return CheckResult("volume_vs_baseline", True, "error", f.unique_events, None,
                           f"skipped: {len(prior)} prior succeeded runs, needs {BASELINE_MIN_RUNS}")
    median = statistics.median(prior)
    floor = BASELINE_FLOOR * Fraction(median)  # 0.7 * 1300 in floats is 909.999...
    return CheckResult(
        "volume_vs_baseline", f.unique_events >= floor, "error", f.unique_events, float(floor),
        f"{f.unique_events} events vs median {median:g} of the last {len(prior)} runs"
        f" (floor {floor:g})",
    )


def events_without_venue(f: RunFacts) -> CheckResult:
    return CheckResult("events_without_venue", f.events_without_venue == 0, "warning",
                       f.events_without_venue, 0, f"{f.events_without_venue} events have no venue")


def venues_outside_ca(f: RunFacts) -> CheckResult:
    detail = "; ".join(f.venues_outside_ca) if f.venues_outside_ca else "all venues are in CA"
    return CheckResult("venues_outside_ca", not f.venues_outside_ca, "warning",
                       len(f.venues_outside_ca), 0, detail)


def implausible_onsales(f: RunFacts) -> CheckResult:
    unexpected = {r: n for r, n in f.onsale_nulled.items() if r not in KNOWN_PLACEHOLDERS}
    detail = ", ".join(
        f"{r} {n}" + (" (known)" if r in KNOWN_PLACEHOLDERS else "")
        for r, n in sorted(f.onsale_nulled.items())
    ) or "none nulled"
    return CheckResult("implausible_onsales", not unexpected, "warning",
                       sum(unexpected.values()), 0, detail)


_RULES: tuple[tuple[str, str, Callable[[RunFacts], CheckResult]], ...] = (
    ("fetched_vs_reported", "error", fetched_vs_reported),
    ("unique_vs_total", "error", unique_vs_total),
    ("window_over_cap", "error", window_over_cap),
    ("volume_vs_baseline", "error", volume_vs_baseline),
    ("events_without_venue", "warning", events_without_venue),
    ("venues_outside_ca", "warning", venues_outside_ca),
    ("implausible_onsales", "warning", implausible_onsales),
)
CHECKS = tuple((name, severity) for name, severity, _ in _RULES)


def run_checks(f: RunFacts) -> list[CheckResult]:
    return [rule(f) for _, _, rule in _RULES]
