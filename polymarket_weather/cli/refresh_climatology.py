"""Refresh per-station day-of-year TMAX climatology from observations."""

from __future__ import annotations

import argparse

from ..data.climatology import refresh_climatology
from ..db import init_schema_and_seed
from ._common import add_common_args, configure_logging, parse_stations


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    add_common_args(p, with_date=False)
    p.add_argument(
        "--lookback-days",
        type=int,
        default=365 * 10,
        help="Window of obs to use (default 10 years).",
    )
    p.add_argument("--no-migrate", action="store_true")
    args = p.parse_args()
    configure_logging(args.verbose)

    if not args.no_migrate:
        init_schema_and_seed()

    counts = refresh_climatology(
        parse_stations(args.station),
        lookback_days=args.lookback_days,
    )
    print(f"Climatology rows touched: {counts['climatology']}")


if __name__ == "__main__":
    main()
