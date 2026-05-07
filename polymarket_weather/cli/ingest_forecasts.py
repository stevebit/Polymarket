"""Ingest Open-Meteo (multi-model) and NWS gridpoint forecasts."""

from __future__ import annotations

import argparse

from ..data.forecasts import ingest_forecasts
from ..db import init_schema_and_seed
from ._common import add_common_args, configure_logging, parse_stations


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    add_common_args(p, with_date=False)
    p.add_argument("--past-days", type=int, default=7)
    p.add_argument("--forecast-days", type=int, default=8)
    p.add_argument("--no-migrate", action="store_true")
    args = p.parse_args()
    configure_logging(args.verbose)

    if not args.no_migrate:
        init_schema_and_seed()

    counts = ingest_forecasts(
        parse_stations(args.station),
        past_days=args.past_days,
        forecast_days=args.forecast_days,
    )
    print(f"Persisted forecasts rows: {counts['forecasts']}")


if __name__ == "__main__":
    main()
