"""Backfill hourly METAR (Iowa Mesonet) into ``hourly_observations``.

Includes optional **neighbor airports** per city (see
``polymarket_weather.stations.NEIGHBOR_ICAOS_BY_SLUG``) for spatial context.

Does not change Polymarket resolution (still daily WU TMAX at the primary ICAO).
"""

from __future__ import annotations

import argparse
import datetime as dt

from ..data.hourly_observations import ingest_hourly_observations
from ..db import init_schema_and_seed
from ._common import add_common_args, configure_logging, parse_stations


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    add_common_args(p, with_date=False)
    p.add_argument(
        "--start",
        type=lambda s: dt.date.fromisoformat(s),
        required=True,
        help="First calendar day (UTC window on Mesonet) to fetch.",
    )
    p.add_argument(
        "--end",
        type=lambda s: dt.date.fromisoformat(s),
        default=dt.date.today(),
        help="Last calendar day (inclusive).",
    )
    p.add_argument(
        "--chunk-days",
        type=int,
        default=120,
        help="Split long ranges into chunks for Mesonet (default 120).",
    )
    p.add_argument("--no-migrate", action="store_true")
    args = p.parse_args()
    configure_logging(args.verbose)

    if not args.no_migrate:
        init_schema_and_seed()

    out = ingest_hourly_observations(
        parse_stations(args.station),
        start_date=args.start,
        end_date=args.end,
        chunk_days=args.chunk_days,
    )
    print(f"Upserted {out['hourly_observations']} hourly_observations rows")


if __name__ == "__main__":
    main()
