"""Check rules (PLAN §5). Pure: they read a snapshot of the run's facts, never the network or DB.

Severity "error" means the run's data can't be trusted as complete; "warning" means something
upstream looks off. An error-level failure makes `ingest` exit non-zero, which turns the scheduled
GitHub Actions run red and notifies its owner (PLAN §5, alert path).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from marquee.fetch import CAP
from marquee.transform import ONSALE_MAX_LEAD

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
    if bad:
        detail = "; ".join(f"the {_day(w.label)} window returned {w.fetched:,} of "
                           f"{w.reported:,} events" for w in bad)
        detail = detail[0].upper() + detail[1:]
    elif gap:
        detail = (f"All {len(f.windows)} windows returned what they reported, within tolerance"
                  f" (largest gap: {_count(gap, 'event')})")
    else:
        detail = f"All {len(f.windows)} windows returned every event they reported"
    return CheckResult("fetched_vs_reported", not bad, "error", len(bad), 0, detail)


def unique_vs_total(f: RunFacts) -> CheckResult:
    if f.total_reported is None:
        return CheckResult("unique_vs_total", False, "error", f.unique_window_ids, None,
                           "The API's total for the whole range couldn't be read")
    diff = f.unique_window_ids - f.total_reported
    allowed = tolerance(f.total_reported)
    if diff == 0:
        detail = f"{f.unique_window_ids:,} received, exactly the API's total for the range"
    else:
        detail = (f"{f.unique_window_ids:,} received vs the API's total of {f.total_reported:,}:"
                  f" {abs(diff):,} {'fewer' if diff < 0 else 'more'}"
                  f" (up to {allowed:,} allowed for drift)")
    return CheckResult("unique_vs_total", abs(diff) <= allowed, "error", f.unique_window_ids,
                       f.total_reported, detail)


def window_over_cap(f: RunFacts) -> CheckResult:
    over = [w for w in f.windows if w.reported > CAP]
    detail = (f"Over the API's {CAP:,}-event cap, so some events can't be fetched: "
              + ", ".join(f"{_day(w.label)} ({w.reported:,} events)" for w in over) if over
              else f"No window over the API's {CAP:,}-event cap")
    return CheckResult("window_over_cap", not over, "error", len(over), 0, detail)


def volume_vs_baseline(f: RunFacts) -> CheckResult:
    prior = f.prior_unique[:BASELINE_RUNS]
    if len(prior) < BASELINE_MIN_RUNS:
        return CheckResult("volume_vs_baseline", True, "error", f.unique_events, None,
                           f"Skipped: needs {BASELINE_MIN_RUNS} earlier good runs to compare"
                           f" with, has {len(prior)}")
    median = statistics.median(prior)
    floor = BASELINE_FLOOR * Fraction(median)  # 0.7 * 1300 in floats is 909.999...
    alert_at = math.ceil(floor) - 1  # the largest count below the floor: 912 for 912.1
    return CheckResult(
        "volume_vs_baseline", f.unique_events >= floor, "error", f.unique_events, float(floor),
        f"{f.unique_events:,} events vs a typical {median:,.0f} (alert at {alert_at:,} or fewer)",
    )


def events_without_venue(f: RunFacts) -> CheckResult:
    n = f.events_without_venue
    detail = (f"All {f.unique_events:,} events have a venue" if n == 0
              else f"{_count(n, 'event')} {'has' if n == 1 else 'have'} no venue")
    return CheckResult("events_without_venue", n == 0, "warning", n, 0, detail)


def venues_outside_ca(f: RunFacts) -> CheckResult:
    far = f.venues_outside_ca
    detail = (f"{_count(len(far), 'venue')} outside California: " + "; ".join(far) if far
              else "Every venue is in California")
    return CheckResult("venues_outside_ca", not f.venues_outside_ca, "warning",
                       len(f.venues_outside_ca), 0, detail)


def implausible_onsales(f: RunFacts) -> CheckResult:
    unexpected = {r: n for r, n in f.onsale_nulled.items() if r not in KNOWN_PLACEHOLDERS}
    known = {r: n for r, n in f.onsale_nulled.items() if r in KNOWN_PLACEHOLDERS}
    # Problems first, then the expected placeholder.
    detail = "; ".join([*(_onsale_words(r, n) for r, n in sorted(unexpected.items())),
                        *(_onsale_words(r, n) + " (expected)" for r, n in sorted(known.items()))]
                       ) or "Every onsale date is plausible"
    return CheckResult("implausible_onsales", not unexpected, "warning",
                       sum(unexpected.values()), 0, detail)


_YEARS = ONSALE_MAX_LEAD.days // 365
_ONSALE_REASONS = {  # reason: (one event, several events)
    "placeholder_1900": ("uses Ticketmaster's 1900 placeholder date",
                         "use Ticketmaster's 1900 placeholder date"),
    "after_event": ("has an onsale after the show itself", "have an onsale after the show itself"),
    "too_early": (f"has an onsale more than {_YEARS} years before the show",
                  f"have an onsale more than {_YEARS} years before the show"),
    "too_late": (f"has an onsale more than {_YEARS} years away",
                 f"have an onsale more than {_YEARS} years away"),
    "unparseable": ("has an onsale date that couldn't be read",
                    "have an onsale date that couldn't be read"),
}


def _onsale_words(reason: str, n: int) -> str:
    # too_late only happens to undated events (transform), so it says so.
    subject = _count(n, "undated event" if reason == "too_late" else "event")
    words = _ONSALE_REASONS.get(reason)
    return f"{subject} {words[0] if n == 1 else words[1]}" if words else f"{subject}: {reason}"


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}" if n == 1 else f"{n:,} {noun}s"


def _day(label: str) -> str:
    """A window's label is its first LA day, '2026-10-13': shown as 'Oct 13'."""
    try:
        day = date.fromisoformat(label)
    except ValueError:
        return label
    return f"{day:%b} {day.day}"


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
