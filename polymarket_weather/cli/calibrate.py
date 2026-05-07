"""Calibration / backtest. Writes a markdown report under ``reports/`` and a
row to ``calibration_runs``."""

from __future__ import annotations

import argparse

from ..calibration import run_calibration
from ..db import init_schema_and_seed
from ..models.baseline import MODEL_M0, MODEL_M1
from ._common import add_common_args, configure_logging, parse_stations


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    add_common_args(p, with_date=False)
    p.add_argument(
        "--model",
        default=f"{MODEL_M0},{MODEL_M1}",
        help="Comma-separated model_id list. One report per model.",
    )
    p.add_argument(
        "--lookback-days",
        type=int,
        default=365,
        help="How far back to look for resolved events.",
    )
    p.add_argument("--no-migrate", action="store_true")
    args = p.parse_args()
    configure_logging(args.verbose)

    if not args.no_migrate:
        init_schema_and_seed()

    stations = parse_stations(args.station)
    for model_id in (m.strip() for m in args.model.split(",") if m.strip()):
        result = run_calibration(
            model_id,
            station_slugs=stations,
            lookback_days=args.lookback_days,
        )
        print(
            f"[{model_id}] run_id={result.run_id} sample_n={result.sample_n} "
            f"log_loss={result.log_loss} brier={result.brier}"
        )
        print(f"  report: {result.report_path}")


if __name__ == "__main__":
    main()
