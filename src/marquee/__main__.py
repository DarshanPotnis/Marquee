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
    if settings.tm_api_key is None:
        log.error("config: TM_API_KEY is not set")
        return 2
    with db.connect(settings.database_url) as conn, make_client(settings) as client:
        s = ingest(conn, client, now=utc_now())
    if s is None:
        log.info("ingest: another ingest is running (lock 'marquee.ingest' is held); "
                 "nothing to do")
        return 0
    level = {"succeeded": logging.INFO, "partial": logging.WARNING}.get(s.status, logging.ERROR)
    nulled = ", ".join(f"{k} {v}" for k, v in sorted(s.onsale_nulled.items())) or "none"
    log.log(
        level,
        "ingest: run %d %s: %s windows (%s split), %d calls; reported %s, fetched %s, "
        "unique %d (%d undated); %d changes; onsale nulled: %s; budget left %s%s",
        s.run_id, s.status, s.windows, s.splits, s.api_calls, s.reported_total,
        s.fetched_total, s.unique_events, s.undated_events, s.changes, nulled, s.budget_left,
        f"; error: {s.error}" if s.error else "",
    )
    return 1 if s.status == "failed" else 0


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
