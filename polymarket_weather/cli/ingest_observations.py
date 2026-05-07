"""Ingest NOAA GHCN-Daily TMAX observations.

Defaults to the trailing 30 days. Use ``--years 10`` for the initial backfill.
"""

from __future__ import annotations

import argparse
import datetime as dt

from ..data.observations import ingest_observations
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
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--years", type=int, default=None)
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

    print(f"Ingesting NOAA GHCN-D TMAX [{start} .. {args.end}]")
    counts = ingest_observations(parse_stations(args.station), start, args.end)
    print(f"Persisted observations rows: {counts['observations']}")


if __name__ == "__main__":
    main()
