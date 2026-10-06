"""Command line: `python -m marquee <command>`."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from marquee import db
from marquee.config import ConfigError, Settings, settings_from_environment

log = logging.getLogger("marquee")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m marquee")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply pending SQL files from migrations/")
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


if __name__ == "__main__":
    sys.exit(main())
