"""Generate a standalone HTML dashboard for raw weather data.

The output file embeds station metadata, observations, and forecasts so it can
be opened directly in a browser without running a local web server.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import config
from ..dashboard import generate_dashboard_html
from ..db import init_schema_and_seed
from ._common import add_common_args, configure_logging, parse_stations


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    add_common_args(p, with_date=False)
    p.add_argument(
        "--lookback-days",
        type=int,
        default=365,
        help="Include observations/forecasts newer than today - N days.",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=config.paths().reports / "weather_dashboard.html",
        help="Output HTML file path.",
    )
    p.add_argument(
        "--all-forecast-runs",
        action="store_true",
        help="Include all forecast runs (default keeps latest run per source/date).",
    )
    p.add_argument("--no-migrate", action="store_true")
    args = p.parse_args()

    configure_logging(args.verbose)
    if not args.no_migrate:
        init_schema_and_seed()

    stations = parse_stations(args.station)
    dataset = generate_dashboard_html(
        station_slugs=stations,
        lookback_days=args.lookback_days,
        output_path=args.output,
        all_forecast_runs=args.all_forecast_runs,
    )
    print(
        f"Dashboard written: {args.output} "
        f"(stations={dataset.get('station_count', 0)}, lookback_days={args.lookback_days})"
    )


if __name__ == "__main__":
    main()

