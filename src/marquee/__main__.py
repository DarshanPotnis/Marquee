"""Command line: `python -m marquee <command>`."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from marquee import db
from marquee.config import ConfigError, Settings, settings_from_environment
from marquee.fetch import BASE_QUERY, PAGE_SIZE, fetch_window
from marquee.tm_client import TicketmasterClient, TicketmasterError
from marquee.windows import API_TIME_FORMAT, api_time, plan_range

log = logging.getLogger("marquee")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m marquee")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply pending SQL files from migrations/")
    brute = commands.add_parser(
        "brute-force", help="read-only: one search over the whole 90-day range, paged to the end"
    )
    brute.add_argument(
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


def _utc(text: str) -> datetime:
    try:
        return datetime.strptime(text, API_TIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DDTHH:MM:SSZ, got {text!r}") from None


if __name__ == "__main__":
    sys.exit(main())
