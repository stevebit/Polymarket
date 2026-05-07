"""Run M0 + M1 predictions for the configured stations and target dates."""

from __future__ import annotations

import argparse
import datetime as dt

from ..db import init_schema_and_seed
from ..models.baseline import MODEL_M0, MODEL_M1, run_predictions
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
        default=f"{MODEL_M0},{MODEL_M1}",
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
    counts = run_predictions(
        parse_stations(args.station), targets, models=models
    )
    print(
        f"Wrote predictions={counts['predictions']} "
        f"bucket_probs={counts['bucket_probs']} "
        f"models={list(models)} dates=[{targets[0]}..{targets[-1]}]"
    )


if __name__ == "__main__":
    main()
