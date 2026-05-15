"""Argparse helpers shared across the CLI modules."""

from __future__ import annotations

import argparse
import datetime as dt
import logging

from .. import config
from ..stations import REGISTRY


def parse_cli_date(s: str) -> dt.date:
    """Parse ``YYYY-MM-DD`` or the keywords ``today`` / ``yesterday``.

    Keywords use the **UTC** calendar date so batch jobs match
    ``--cutoff-hour`` conventions regardless of the runner's local TZ.
    """
    key = s.strip().lower()
    if key == "today":
        return dt.datetime.now(dt.timezone.utc).date()
    if key == "yesterday":
        return dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    return dt.date.fromisoformat(s.strip())


def parse_date(s: str) -> dt.date:
    return parse_cli_date(s)


def add_common_args(p: argparse.ArgumentParser, *, with_date: bool = True) -> None:
    p.add_argument(
        "--station",
        default=",".join(config.station_slugs()),
        help=(
            "Comma-separated station slugs (default from WEATHER_STATIONS env). "
            f"Known: {','.join(sorted(REGISTRY))}"
        ),
    )
    if with_date:
        p.add_argument(
            "--date",
            type=parse_date,
            default=dt.datetime.now(dt.timezone.utc).date(),
            help="Anchor date: YYYY-MM-DD, today, or yesterday (UTC; default today UTC).",
        )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose logging.",
    )


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def parse_stations(arg: str) -> list[str]:
    return [s.strip() for s in arg.split(",") if s.strip() and s.strip() in REGISTRY]
