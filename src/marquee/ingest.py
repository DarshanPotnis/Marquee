"""One ingest run (PLAN §2): lock, close abandoned runs, open the run, fetch, save and load page by
page, check, prune, close the run.

Each page's raw is committed first, on its own, so it survives whatever happens next. Then one
transaction transforms, detects changes and upserts: the page's rows fully land or not at all, so
a run that stops halfway is always safe to re-run. A page the database refuses is recorded and
skipped; the run carries on, then closes as failed.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from marquee.checks import BASELINE_RUNS, CheckResult, RunFacts, WindowFacts, run_checks
from marquee.db import INGEST_LOCK, Connection, LockHeld, advisory_lock
from marquee.fetch import BASE_QUERY, CAP, RangeResult, fetch_range, fetch_window
from marquee.load import SOURCE, load_rows, save_raw
from marquee.prune import prune
from marquee.runs import ABANDONED, ABANDONED_AFTER, GOOD_RUN
from marquee.tm_client import BudgetExhausted, Page, TicketmasterClient, TicketmasterError
from marquee.transform import transform_page
from marquee.windows import Window, api_time, plan_range, plan_windows

log = logging.getLogger(__name__)

# Undated events never match a date-filtered search; the two flags can't be combined (api-notes §8).
UNDATED_FLAGS = ("includeTBA", "includeTBD")


@dataclass(frozen=True)
class RunSummary:
    run_id: int
    status: str  # succeeded | partial | failed
    windows: int | None  # final windows
    splits: int | None
    api_calls: int
    reported_total: int | None  # final windows only
    fetched_total: int | None  # final windows only, edge overlaps included
    unique_events: int  # distinct events loaded this run, undated included
    probe_calls: int | None
    probe_events: int | None
    undated_events: int
    onsale_nulled: dict[str, int]
    budget_left: int | None
    changes: int
    error: str | None
    checks: tuple[CheckResult, ...] = ()  # empty when the run didn't complete
    pruned_raw: int = 0

    @property
    def checks_failed(self) -> int:
        return sum(1 for c in self.checks if c.severity == "error" and not c.passed)

    @property
    def warnings(self) -> int:
        return sum(1 for c in self.checks if c.severity == "warning" and not c.passed)


@dataclass
class _Tally:
    ids: set[str] = field(default_factory=set)
    undated_ids: set[str] = field(default_factory=set)
    onsale_nulled: Counter[str] = field(default_factory=Counter)
    refused: list[str] = field(default_factory=list)  # "raw 123: DataError: ..." per page


def ingest(
    conn: Connection,
    client: TicketmasterClient,
    *,
    now: datetime,
    split_threshold: int = CAP,
    retention_days: int = 3,
) -> RunSummary | None:
    """Run once. Returns None, having done nothing, if another ingest holds the lock."""
    try:
        with advisory_lock(conn, INGEST_LOCK):
            return _run(conn, client, now, split_threshold, retention_days)
    except LockHeld:
        return None


def _run(
    conn: Connection, client: TicketmasterClient, now: datetime, split_threshold: int,
    retention_days: int,
) -> RunSummary:
    _close_abandoned(conn)
    whole = plan_range(now)
    opened = conn.execute(
        "INSERT INTO ingest_runs (source, range_start, range_end) VALUES (%s, %s, %s)"
        " RETURNING run_id, started_at", (SOURCE, whole.start, whole.end)).fetchone()
    assert opened is not None
    run_id, started_at = opened
    calls_before = client.calls
    tally = _Tally()

    def on_page(window: Window | None, page: Page) -> None:
        with conn.transaction():  # raw first, on its own: it survives a page that fails to load
            raw_id = save_raw(conn, run_id, page)
        # The run's own start time, so a rebuild applies the onsale rule exactly the same way.
        rows = transform_page(page.body, run_at=started_at)
        try:
            with conn.transaction():
                load_rows(conn, run_id, rows)
        except psycopg.Error as exc:
            if conn.broken:
                raise  # the connection is gone, not just this page: the run fails
            tally.refused.append(f"raw {raw_id}: {type(exc).__name__}: {exc}")
            log.error("ingest: the database refused raw %d (kept for rebuild): %s", raw_id, exc)
            return
        ids = {e.source_id for e in rows.events}
        tally.ids |= ids
        if window is None:
            tally.undated_ids |= ids
        tally.onsale_nulled.update(rows.onsale_nulled)

    result: RangeResult | None = None
    total_reported: int | None = None
    status, error = "succeeded", None
    try:
        result = fetch_range(client, BASE_QUERY, plan_windows(now),
                             split_threshold=split_threshold, on_page=on_page)
        # The API's total for the whole range, read right after the windows, for unique_vs_total.
        # A count, not data: not saved as raw.
        total = client.search_events({**BASE_QUERY, "startDateTime": api_time(whole.start),
                                      "endDateTime": api_time(whole.end), "size": "1"})
        total_reported = int(total.body["page"]["totalElements"])
        for flag in UNDATED_FLAGS:
            fetch_window(client, {**BASE_QUERY, flag: "only"}, None, on_page=on_page)
    except BudgetExhausted as exc:
        status, error = "partial", str(exc)
    except (TicketmasterError, psycopg.Error) as exc:
        status, error = "failed", f"{type(exc).__name__}: {exc}"
    except BaseException as exc:
        # A bug, not an outage: close the run as failed, then let it surface with its traceback.
        _close(conn, run_id, "failed", f"{type(exc).__name__}: {exc}", result, tally,
               client.calls - calls_before, client.budget_left, (), 0)
        raise
    if status == "succeeded" and tally.refused:
        status = "failed"
        error = (f"the database refused {len(tally.refused)} page(s), raw kept; first: "
                 f"{tally.refused[0]}")
    # Checks judge a complete run; a partial or failed run is already a bad run on its own.
    checks: tuple[CheckResult, ...] = ()
    if status == "succeeded" and result is not None:
        checks = tuple(run_checks(_facts(conn, run_id, result, total_reported, tally)))
        _save_checks(conn, run_id, checks)
    pruned = prune(conn, retention_days=retention_days)
    return _close(conn, run_id, status, error, result, tally, client.calls - calls_before,
                  client.budget_left, checks, pruned.raw_rows)


def _facts(
    conn: Connection, run_id: int, result: RangeResult, total_reported: int | None, tally: _Tally
) -> RunFacts:
    prior = conn.execute(
        f"SELECT unique_events FROM ingest_runs WHERE {GOOD_RUN} AND run_id < %s"
        " AND unique_events IS NOT NULL ORDER BY run_id DESC LIMIT %s", (run_id, BASELINE_RUNS),
    ).fetchall()
    no_venue = conn.execute(
        "SELECT count(*) FROM events WHERE last_seen_run = %s AND venue_id IS NULL", (run_id,),
    ).fetchone()
    outside = conn.execute(
        """SELECT DISTINCT v.name, v.city, v.state FROM events e JOIN venues v USING (venue_id)
           WHERE e.last_seen_run = %s AND v.state IS DISTINCT FROM 'CA' ORDER BY 1""", (run_id,),
    ).fetchall()
    return RunFacts(
        windows=tuple(WindowFacts(w.window.first_day.isoformat() if w.window else "undated",
                                  w.reported, w.fetched) for w in result.final),
        unique_window_ids=len(result.unique_ids),
        total_reported=total_reported,
        unique_events=len(tally.ids),
        prior_unique=tuple(int(r[0]) for r in prior),
        events_without_venue=int(no_venue[0]) if no_venue else 0,
        venues_outside_ca=tuple(f"{name} ({city}, {state})" for name, city, state in outside),
        onsale_nulled=dict(tally.onsale_nulled),
    )


def _close_abandoned(conn: Connection) -> None:
    """Close runs left "running" past the workflow's timeout. Called holding the ingest lock, so
    no live run can be among them."""
    minutes = int(ABANDONED_AFTER.total_seconds() // 60)
    closed = conn.execute(
        "UPDATE ingest_runs SET status = 'failed', finished_at = now(), error = %s"
        " WHERE status = 'running' AND started_at < now() - %s RETURNING run_id",
        (f"{ABANDONED} still running {minutes}+ minutes after it started (killed, timed out or"
         " lost its database connection); closed by the next ingest", ABANDONED_AFTER),
    ).fetchall()
    if closed:
        log.warning("ingest: closed %d abandoned run(s) as failed: %s", len(closed),
                    ", ".join(str(r[0]) for r in closed))


def _save_checks(conn: Connection, run_id: int, checks: tuple[CheckResult, ...]) -> None:
    with conn.transaction():
        conn.cursor().executemany(
            """INSERT INTO check_results
                 (run_id, check_name, passed, observed, expected, detail, severity)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            [(run_id, c.name, c.passed, c.observed, c.expected, c.detail, c.severity)
             for c in checks],
        )


def _close(
    conn: Connection, run_id: int, status: str, error: str | None, result: RangeResult | None,
    tally: _Tally, api_calls: int, budget_left: int | None, checks: tuple[CheckResult, ...],
    pruned_raw: int,
) -> RunSummary:
    summary = RunSummary(
        run_id=run_id, status=status,
        windows=len(result.final) if result else None,
        splits=result.splits if result else None,
        api_calls=api_calls,
        reported_total=result.reported if result else None,
        fetched_total=result.fetched if result else None,
        unique_events=len(tally.ids),
        probe_calls=result.probe_calls if result else None,
        probe_events=result.probe_events if result else None,
        undated_events=len(tally.undated_ids),
        onsale_nulled=dict(tally.onsale_nulled),
        budget_left=budget_left,
        changes=_count_changes(conn, run_id),
        error=error,
        checks=checks,
        pruned_raw=pruned_raw,
    )
    conn.execute(
        """UPDATE ingest_runs SET finished_at = now(), status = %s, windows = %s, api_calls = %s,
             reported_total = %s, fetched_total = %s, unique_events = %s, budget_left = %s,
             error = %s, probe_calls = %s, probe_events = %s, undated_events = %s,
             onsale_nulled = %s, checks_failed = %s, pruned_raw = %s
           WHERE run_id = %s""",
        (summary.status, summary.windows, summary.api_calls, summary.reported_total,
         summary.fetched_total, summary.unique_events, summary.budget_left, summary.error,
         summary.probe_calls, summary.probe_events, summary.undated_events,
         Jsonb(summary.onsale_nulled), summary.checks_failed if checks else None,
         summary.pruned_raw, run_id),
    )
    return summary


def _count_changes(conn: Connection, run_id: int) -> int:
    row = conn.execute("SELECT count(*) FROM event_changes WHERE run_id = %s", (run_id,)).fetchone()
    return int(row[0]) if row else 0
