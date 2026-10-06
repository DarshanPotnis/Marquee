"""Prune raw responses older than RAW_RETENTION_DAYS (PLAN §8). I/O.

Deletes whole runs, so a run is never half pruned. Always keeps the most recent succeeded run's
raw, however old, so a rebuild has one complete run even if the scheduler stopped for a week. Run
records and check results are kept: they're tiny, and they are the history.
The cutoff uses the database's clock, the same one that stamped ingest_runs.started_at.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from marquee.db import Connection


@dataclass(frozen=True)
class PruneResult:
    cutoff: datetime
    runs: tuple[int, ...]  # runs whose raw was (or, in a dry run, would be) deleted
    raw_rows: int
    raw_bytes: int  # stored (gzip) bytes
    kept_run: int | None  # the latest succeeded run, always kept


def prune(conn: Connection, *, retention_days: int, dry_run: bool = False) -> PruneResult:
    cutoff_row = conn.execute("SELECT now() - make_interval(days => %s)",
                              (retention_days,)).fetchone()
    kept_row = conn.execute("SELECT max(run_id) FROM ingest_runs WHERE status = 'succeeded'"
                            ).fetchone()
    assert cutoff_row is not None and kept_row is not None
    cutoff, kept = cutoff_row[0], kept_row[0]
    targets = conn.execute(
        """SELECT r.run_id, count(*), coalesce(sum(octet_length(r.body_gzip)), 0)
           FROM raw_responses r JOIN ingest_runs i USING (run_id)
           WHERE i.started_at < %s AND r.run_id IS DISTINCT FROM %s
           GROUP BY r.run_id ORDER BY r.run_id""",
        (cutoff, kept),
    ).fetchall()
    runs = tuple(int(t[0]) for t in targets)
    if runs and not dry_run:
        conn.execute("DELETE FROM raw_responses WHERE run_id = ANY(%s)", (list(runs),))
    return PruneResult(cutoff, runs, sum(int(t[1]) for t in targets),
                       sum(int(t[2]) for t in targets), kept)
