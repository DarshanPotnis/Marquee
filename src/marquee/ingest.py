"""One ingest run (PLAN §2): lock, open the run, fetch, save and load page by page, close the run.

Each page is one transaction: save raw, transform, detect changes, upsert. A page either fully
lands or not at all, so a run that stops halfway is always safe to re-run.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from marquee.db import INGEST_LOCK, Connection, LockHeld, advisory_lock
from marquee.fetch import BASE_QUERY, CAP, RangeResult, WindowResult, fetch_range, fetch_window
from marquee.load import SOURCE, load_rows, save_raw
from marquee.tm_client import BudgetExhausted, Page, TicketmasterClient, TicketmasterError
from marquee.transform import transform_page
from marquee.windows import Window, plan_windows

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


@dataclass
class _Tally:
    ids: set[str] = field(default_factory=set)
    undated_ids: set[str] = field(default_factory=set)
    onsale_nulled: Counter[str] = field(default_factory=Counter)


def ingest(
    conn: Connection, client: TicketmasterClient, *, now: datetime, split_threshold: int = CAP
) -> RunSummary | None:
    """Run once. Returns None, having done nothing, if another ingest holds the lock."""
    try:
        with advisory_lock(conn, INGEST_LOCK):
            return _run(conn, client, now, split_threshold)
    except LockHeld:
        return None


def _run(
    conn: Connection, client: TicketmasterClient, now: datetime, split_threshold: int
) -> RunSummary:
    opened = conn.execute("INSERT INTO ingest_runs (source) VALUES (%s) RETURNING run_id,"
                          " started_at", (SOURCE,)).fetchone()
    assert opened is not None
    run_id, started_at = opened
    calls_before = client.calls
    tally = _Tally()

    def on_page(window: Window | None, page: Page) -> None:
        # The run's own start time, so a rebuild applies the onsale rule exactly the same way.
        rows = transform_page(page.body, run_at=started_at)
        with conn.transaction():
            save_raw(conn, run_id, page)
            load_rows(conn, run_id, rows)
        ids = {e.source_id for e in rows.events}
        tally.ids |= ids
        if window is None:
            tally.undated_ids |= ids
        tally.onsale_nulled.update(rows.onsale_nulled)

    result: RangeResult | None = None
    undated: list[WindowResult] = []
    status, error = "succeeded", None
    try:
        result = fetch_range(client, BASE_QUERY, plan_windows(now),
                             split_threshold=split_threshold, on_page=on_page)
        for flag in UNDATED_FLAGS:
            undated.append(fetch_window(client, {**BASE_QUERY, flag: "only"}, None,
                                        on_page=on_page))
    except BudgetExhausted as exc:
        status, error = "partial", str(exc)
    except (TicketmasterError, psycopg.Error) as exc:
        status, error = "failed", f"{type(exc).__name__}: {exc}"
    except BaseException as exc:
        # A bug, not an outage: close the run as failed, then let it surface with its traceback.
        _close(conn, run_id, "failed", f"{type(exc).__name__}: {exc}", result, tally,
               client.calls - calls_before, client.budget_left)
        raise
    return _close(conn, run_id, status, error, result, tally, client.calls - calls_before,
                  client.budget_left)


def _close(
    conn: Connection, run_id: int, status: str, error: str | None, result: RangeResult | None,
    tally: _Tally, api_calls: int, budget_left: int | None,
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
    )
    conn.execute(
        """UPDATE ingest_runs SET finished_at = now(), status = %s, windows = %s, api_calls = %s,
             reported_total = %s, fetched_total = %s, unique_events = %s, budget_left = %s,
             error = %s, probe_calls = %s, probe_events = %s, undated_events = %s,
             onsale_nulled = %s
           WHERE run_id = %s""",
        (summary.status, summary.windows, summary.api_calls, summary.reported_total,
         summary.fetched_total, summary.unique_events, summary.budget_left, summary.error,
         summary.probe_calls, summary.probe_events, summary.undated_events,
         Jsonb(summary.onsale_nulled), run_id),
    )
    return summary


def _count_changes(conn: Connection, run_id: int) -> int:
    row = conn.execute("SELECT count(*) FROM event_changes WHERE run_id = %s", (run_id,)).fetchone()
    return int(row[0]) if row else 0
