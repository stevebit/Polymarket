"""Ensemble forecast ingest (Open-Meteo ensemble endpoint).

Pulls per-member daily TMAX from the GFS, ECMWF IFS, ICON and GEM ensembles
and persists into ``forecast_members``. Also writes ensemble-mean rows into
``forecasts`` so existing baseline / EMOS code paths see them as additional
sources without joining to the per-member table.
"""

from __future__ import annotations

import argparse

from ..data.ensembles import ingest_ensembles
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

    stations = parse_stations(args.station)
    counts = ingest_ensembles(
        stations,
        past_days=args.past_days,
        forecast_days=args.forecast_days,
    )
    print(
        f"Wrote forecast_members={counts['forecast_members']} "
        f"forecasts(ens means)={counts['forecasts']} "
        f"stations={stations}"
    )


if __name__ == "__main__":
    main()
