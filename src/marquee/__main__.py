"""Command line: `python -m marquee <command>`."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from marquee import db
from marquee.config import ConfigError, Settings, settings_from_environment
from marquee.fetch import BASE_QUERY, CAP, PAGE_SIZE, fetch_range, fetch_window
from marquee.ingest import ingest
from marquee.prune import prune
from marquee.rebuild import RebuildError, rebuild, verify
from marquee.tm_client import TicketmasterClient, TicketmasterError
from marquee.windows import API_TIME_FORMAT, api_time, plan_range, plan_windows

log = logging.getLogger("marquee")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m marquee")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply pending SQL files from migrations/")
    brute = commands.add_parser(
        "brute-force", help="read-only: one search over the whole 90-day range, paged to the end"
    )
    windowed = commands.add_parser(
        "fetch-windows", help="read-only: the same range in LA-day windows, split as needed"
    )
    windowed.add_argument(
        "--split-threshold", type=_positive, default=CAP, metavar="N",
        help=f"split windows whose page 0 reports more than N events (default {CAP})",
    )
    commands.add_parser(
        "ingest", help="fetch every window plus undated events; save raw; load the clean tables"
    )
    checks_cmd = commands.add_parser(
        "checks", help="report a run's check results (default: the latest)"
    )
    checks_cmd.add_argument("--run", type=_positive, metavar="N", help="run id")
    prune_cmd = commands.add_parser(
        "prune", help="delete raw responses older than RAW_RETENTION_DAYS, by whole run"
    )
    prune_cmd.add_argument("--dry-run", action="store_true", help="report only; delete nothing")
    rebuild_cmd = commands.add_parser(
        "rebuild", help="replay retained raw onto the clean tables; 0 API calls"
    )
    rebuild_cmd.add_argument(
        "--verify", action="store_true",
        help="rebuild into a throwaway schema and compare with the live tables; changes nothing",
    )
    for command in (brute, windowed):
        # Pass the same --start to both, back to back, for a like-for-like comparison.
        command.add_argument(
            "--start", type=_utc, help="range start, YYYY-MM-DDTHH:MM:SSZ (default: now)"
        )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        settings = settings_from_environment()
    except ConfigError as exc:
        log.error("config: %s", exc)
        return 2
    if args.command == "migrate":
        return run_migrate(settings)
    if args.command == "brute-force":
        return run_brute_force(settings, args.start)
    if args.command == "ingest":
        return run_ingest(settings)
    if args.command == "checks":
        return run_checks_report(settings, args.run)
    if args.command == "prune":
        return run_prune(settings, dry_run=args.dry_run)
    if args.command == "rebuild":
        return run_rebuild(settings, verify_only=args.verify)
    if args.command == "fetch-windows":
        return run_fetch_windows(settings, args.start, args.split_threshold)
    parser.error(f"unknown command {args.command!r}")


def run_migrate(settings: Settings) -> int:
    try:
        with db.connect(settings.database_url) as conn:
            result = db.migrate(conn)
    except db.LockHeld as exc:
        log.error("migrate: %s. Is another migrate running? Nothing was changed.", exc)
        return 1
    names = f" ({', '.join(result.applied)})" if result.applied else ""
    log.info(
        "migrate: applied %d%s, already applied %d",
        len(result.applied), names, result.already_applied,
    )
    return 0


def make_client(settings: Settings) -> TicketmasterClient:
    # A seam for tests, which swap in a client over a fake transport.
    return TicketmasterClient(settings.tm_api_key or "")


def utc_now() -> datetime:
    return datetime.now(UTC)  # a seam for tests


def run_ingest(settings: Settings) -> int:
    """Exit 1 for any bad run (partial, failed, or an error-level check failed), so a scheduled
    run turns red and GitHub notifies its owner (PLAN §5, alert path)."""
    if settings.tm_api_key is None:
        log.error("config: TM_API_KEY is not set")
        return 2
    with db.connect(settings.database_url) as conn, make_client(settings) as client:
        s = ingest(conn, client, now=utc_now(), retention_days=settings.raw_retention_days)
    if s is None:
        log.info("ingest: another ingest or a rebuild is running (lock 'marquee.ingest' is "
                 "held); nothing to do")
        return 0
    bad = s.status != "succeeded" or s.checks_failed > 0
    level = logging.ERROR if bad else logging.INFO
    nulled = ", ".join(f"{k} {v}" for k, v in sorted(s.onsale_nulled.items())) or "none"
    checks = (f"{len(s.checks)} run, {s.checks_failed} failed, {_n(s.warnings, 'warning')}"
              if s.checks else f"skipped (run {s.status})")
    log.log(
        level,
        "ingest: run %d %s: %s windows (%s split), %d calls; reported %s, fetched %s, "
        "unique %d (%d undated); %d changes; checks: %s; pruned %d raw; onsale nulled: %s; "
        "budget left %s%s",
        s.run_id, s.status, s.windows, s.splits, s.api_calls, s.reported_total,
        s.fetched_total, s.unique_events, s.undated_events, s.changes, checks, s.pruned_raw,
        nulled, s.budget_left, f"; error: {s.error}" if s.error else "",
    )
    for c in s.checks:
        if not c.passed:
            log.log(logging.ERROR if c.severity == "error" else logging.WARNING,
                    "ingest: check %s failed (%s): %s", c.name, c.severity, c.detail)
    return 1 if bad else 0


def run_checks_report(settings: Settings, run_id: int | None) -> int:
    with db.connect(settings.database_url) as conn:
        if run_id is None:
            row = conn.execute("SELECT max(run_id) FROM check_results").fetchone()
            run_id = row[0] if row else None
        rows = conn.execute(
            "SELECT check_name, passed, severity, detail FROM check_results WHERE run_id = %s"
            " ORDER BY severity, check_name", (run_id,)).fetchall() if run_id else []
    if not rows:
        log.error("checks: no check results%s", f" for run {run_id}" if run_id else "")
        return 2
    failed = sum(1 for _, ok, sev, _ in rows if not ok and sev == "error")
    warnings = sum(1 for _, ok, sev, _ in rows if not ok and sev == "warning")
    log.info("checks: run %d: %d checks, %d failed, %s", run_id, len(rows), failed,
             _n(warnings, "warning"))
    for name, ok, severity, detail in rows:
        log.log(logging.INFO if ok else (logging.ERROR if severity == "error" else logging.WARNING),
                "checks: %s: %s (%s): %s", name, "passed" if ok else "FAILED", severity, detail)
    return 0


def run_prune(settings: Settings, *, dry_run: bool) -> int:
    with db.connect(settings.database_url) as conn:
        r = prune(conn, retention_days=settings.raw_retention_days, dry_run=dry_run)
    verb = "would delete" if dry_run else "deleted"
    log.info(
        "prune: cutoff %s (%d days); %s %d raw responses from %d run%s (%.1f KB stored); %s",
        r.cutoff.isoformat(timespec="seconds"), settings.raw_retention_days, verb, r.raw_rows,
        len(r.runs), "" if len(r.runs) == 1 else "s", r.raw_bytes / 1024,
        f"keeping run {r.kept_run} (latest good run, kept up to 14 days)" if r.kept_run
        else "no good run in the last 14 days to keep",
    )
    return 0


def run_rebuild(settings: Settings, *, verify_only: bool) -> int:
    """Zero API calls: no client is ever created here."""
    try:
        with db.connect(settings.database_url) as conn:
            if not verify_only:
                r = rebuild(conn)
                log.info("rebuild: replayed %d raw pages from %d runs onto the live tables; "
                         "event_changes untouched; 0 API calls", r.raw_pages, r.runs)
                return 0
            v = verify(conn)
    except RebuildError as exc:
        log.error("rebuild: %s", exc)
        return 1
    except db.LockHeld:
        log.error("rebuild: an ingest is running (lock 'marquee.ingest' is held); nothing was "
                  "changed. Try again when it finishes.")
        return 1
    for d in v.diffs:
        log.log(logging.INFO if d.only_live == d.only_rebuilt == 0 else logging.ERROR,
                "rebuild --verify: %s: %d only live, %d only rebuilt, %d not reproducible "
                "(pruned)%s", d.table, d.only_live, d.only_rebuilt, d.pruned,
                f" (e.g. {', '.join(d.samples)})" if d.samples else "")
    if v.first_seen_pruned:
        log.info("rebuild --verify: first_seen_run not reproducible (pruned) for %d events, "
                 "first seen in runs whose raw is gone", v.first_seen_pruned)
    log.log(logging.INFO if v.ok else logging.ERROR,
            "rebuild --verify: replayed %d raw pages from %d runs into a throwaway schema; "
            "differences: %s; live tables unchanged; 0 API calls", v.raw_pages, v.runs,
            "none" if v.ok else "see above")
    return 0 if v.ok else 1


def run_brute_force(settings: Settings, start: datetime | None) -> int:
    """The "before" picture: one search over the whole range, paged until the API stops us."""
    if settings.tm_api_key is None:
        log.error("config: TM_API_KEY is not set")
        return 2
    whole = plan_range(start or datetime.now(UTC))
    try:
        with make_client(settings) as client:
            result = fetch_window(client, BASE_QUERY, whole)
    except TicketmasterError as exc:
        log.error("brute-force: %s", exc)
        return 1
    unique = len(set(result.event_ids))
    stop = f"stopped by {result.stopped_by}" if result.stopped_by else "paged to the end"
    log.info(
        "brute-force: one search %s .. %s (Ticketmaster LA market, Music), size %d",
        api_time(whole.start), api_time(whole.end), PAGE_SIZE,
    )
    log.info(
        "brute-force: reported %d, fetched %d (%d unique), lost %d; %s; %d calls",
        result.reported, result.fetched, unique, result.reported - unique, stop, result.calls,
    )
    return 0


def run_fetch_windows(
    settings: Settings, start: datetime | None, split_threshold: int = CAP
) -> int:
    """The "after" picture. Proof: unique event ids across all windows equal the API's total."""
    if settings.tm_api_key is None:
        log.error("config: TM_API_KEY is not set")
        return 2
    whole = plan_range(start or datetime.now(UTC))
    windows = plan_windows(whole.start)
    try:
        with make_client(settings) as client:
            result = fetch_range(client, BASE_QUERY, windows, split_threshold=split_threshold)
            # The reported total for the whole range at this moment: totals drift by the hour.
            check = client.search_events({**BASE_QUERY, "startDateTime": api_time(whole.start),
                                          "endDateTime": api_time(whole.end), "size": "1"})
    except TicketmasterError as exc:
        log.error("fetch-windows: %s", exc)
        return 1
    reported_now = int(check.body["page"]["totalElements"])
    unique = len(result.unique_ids)
    proven = unique == reported_now
    over_cap = ", ".join(
        f"{w.window.first_day if w.window else 'undated'} ({w.reported} reported"
        + (f", stopped by {w.stopped_by})" if w.stopped_by else ")")
        for w in result.over_cap
    ) or "none"
    log.info(
        "fetch-windows: %s .. %s (Ticketmaster LA market, Music), %d weekly windows planned, "
        "split threshold %d",
        api_time(whole.start), api_time(whole.end), len(windows), split_threshold,
    )
    log.info(
        "fetch-windows: %d final windows, %d split (%d probe calls, %d probe events)",
        len(result.final), result.splits, result.probe_calls, result.probe_events,
    )
    log.info(
        "fetch-windows: final windows: reported %d, fetched %d, unique %d",
        result.reported, result.fetched, unique,
    )
    log.log(
        logging.INFO if proven else logging.ERROR,
        "fetch-windows: whole range reported %d now; unique == reported: %s",
        reported_now, "yes" if proven else f"NO ({unique} of {reported_now})",
    )
    log.info(
        "fetch-windows: %d calls (%d for windows + 1 whole-range check); over-cap days: %s",
        result.calls + 1, result.calls, over_cap,
    )
    return 0 if proven else 1


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a positive whole number, got {text!r}")
    return value


def _utc(text: str) -> datetime:
    try:
        return datetime.strptime(text, API_TIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DDTHH:MM:SSZ, got {text!r}") from None


if __name__ == "__main__":
    sys.exit(main())
