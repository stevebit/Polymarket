"""Ingest Weather Underground historical observations for parity checking.

This pulls hourly historical observations from weather.com's station endpoint
for each configured ICAO and aggregates to daily max Fahrenheit, matching the
Polymarket resolution station source.
"""

from __future__ import annotations

import argparse
import datetime as dt

from ..data.resolution_observations import ingest_resolution_observations
from ..db import init_schema_and_seed
from ._common import add_common_args, configure_logging, parse_stations


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    add_common_args(p, with_date=False)
    p.add_argument(
        "--start",
        type=lambda s: dt.date.fromisoformat(s),
        help="Backfill start date (overrides --days / --years).",
    )
    p.add_argument(
        "--end",
        type=lambda s: dt.date.fromisoformat(s),
        default=dt.date.today(),
    )
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--years", type=int, default=None)
    p.add_argument(
        "--api-key",
        default=None,
        help="Optional weather.com API key override.",
    )
    p.add_argument("--no-migrate", action="store_true")
    args = p.parse_args()
    configure_logging(args.verbose)

    if not args.no_migrate:
        init_schema_and_seed()

    if args.start:
        start = args.start
    elif args.years:
        start = args.end - dt.timedelta(days=365 * args.years)
    else:
        start = args.end - dt.timedelta(days=args.days)

    print(f"Ingesting WU historical Tmax [{start} .. {args.end}]")
    counts = ingest_resolution_observations(
        parse_stations(args.station),
        start,
        args.end,
        api_key=args.api_key,
    )
    print(f"Persisted WU observation rows: {counts['observations']}")


if __name__ == "__main__":
    main()
