"""Run M0 / M1 / M2 predictions for the configured stations and target dates."""

from __future__ import annotations

import argparse
import datetime as dt

from ..db import init_schema_and_seed
from ..models.baseline import MODEL_M0, MODEL_M1, run_predictions
from ..models.m2_postprocessed_ensemble import MODEL_M2, run_m2_predictions
from ._common import add_common_args, configure_logging, parse_stations


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    add_common_args(p)
    p.add_argument(
        "--days-ahead",
        type=int,
        default=7,
        help="Predict for [--date, --date + N] (inclusive).",
    )
    p.add_argument(
        "--models",
        default=f"{MODEL_M0},{MODEL_M1},{MODEL_M2}",
        help="Comma-separated model_id list to run.",
    )
    p.add_argument("--no-migrate", action="store_true")
    args = p.parse_args()
    configure_logging(args.verbose)

    if not args.no_migrate:
        init_schema_and_seed()

    targets = [
        args.date + dt.timedelta(days=i) for i in range(args.days_ahead + 1)
    ]
    models = tuple(m.strip() for m in args.models.split(",") if m.strip())
    stations = parse_stations(args.station)

    legacy_models = tuple(m for m in models if m in {MODEL_M0, MODEL_M1})
    total_pred = 0
    total_probs = 0
    if legacy_models:
        counts = run_predictions(stations, targets, models=legacy_models)
        total_pred += counts["predictions"]
        total_probs += counts["bucket_probs"]
    if MODEL_M2 in models:
        counts = run_m2_predictions(stations, targets)
        total_pred += counts["predictions"]
        total_probs += counts["bucket_probs"]

    print(
        f"Wrote predictions={total_pred} bucket_probs={total_probs} "
        f"models={list(models)} dates=[{targets[0]}..{targets[-1]}]"
    )


if __name__ == "__main__":
    main()
