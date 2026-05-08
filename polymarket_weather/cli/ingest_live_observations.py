"""Live ASOS / METAR observations ingest.

Pulls the most recent ~36 hours of station temperatures from
``api.weather.gov/stations/{ICAO}/observations`` (or Iowa Mesonet ASOS as a
fallback), aggregates per-local-day max, and persists with
``source = 'asos:live'`` (or ``'mesonet:asos'``) into ``observations``.

Used as a today-so-far feature for the Phase 2 nowcasting model. **Not** a
resolution source — Polymarket resolves on Wunderground daily TMAX.
"""

from __future__ import annotations

import argparse

from ..data.live_observations import ingest_live_observations
from ..db import init_schema_and_seed
from ._common import add_common_args, configure_logging, parse_stations


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    add_common_args(p, with_date=False)
    p.add_argument(
        "--hours-back",
        type=int,
        default=36,
        help="Pull observations from the last N hours.",
    )
    p.add_argument("--no-migrate", action="store_true")
    args = p.parse_args()
    configure_logging(args.verbose)

    if not args.no_migrate:
        init_schema_and_seed()

    stations = parse_stations(args.station)
    counts = ingest_live_observations(stations, hours_back=args.hours_back)
    print(
        f"Wrote observations={counts['observations']} (live source) "
        f"stations={stations}"
    )


if __name__ == "__main__":
    main()
