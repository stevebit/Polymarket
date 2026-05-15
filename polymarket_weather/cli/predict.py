"""Run M0 / M1 / M2 predictions for the configured stations and target dates.

Live mode (default): ``--date`` defaults to today and ``run_time`` is set to
``now()``.

Historical-replay mode (``--as-of YYYY-MM-DD`` + optional ``--cutoff-hour``):
predictions are produced as if we were standing at ``--as-of`` ``cutoff_hour``
UTC. Every internal SELECT is filtered to ``run_time <= as_of`` and
``ingested_at <= as_of`` so no data inserted after that timestamp can leak
into the prediction. The persisted ``run_time`` is set to ``as_of`` so
calibration/backtest see a consistent temporal anchor.

The Phase 4 ``predict_history`` driver consumes ``--as-of`` to rebuild a
complete history of ``bucket_probs`` for the backtest window.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging

from ..db import init_schema_and_seed
from ..models.baseline import MODEL_M0, MODEL_M1, run_predictions
from ..models.m2_postprocessed_ensemble import MODEL_M2, run_m2_predictions
from ._common import (
    add_common_args,
    configure_logging,
    parse_cli_date,
    parse_stations,
)

log = logging.getLogger(__name__)


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
    p.add_argument(
        "--as-of",
        type=parse_cli_date,
        default=None,
        help=(
            "Historical replay anchor: YYYY-MM-DD or today/yesterday (UTC). "
            "When set, the run uses only data available at as_of T cutoff_hour "
            "UTC and writes predictions/bucket_probs with run_time = that timestamp."
        ),
    )
    p.add_argument(
        "--cutoff-hour",
        type=int,
        default=12,
        help=(
            "UTC hour used together with --as-of (default 12, matches "
            "Polymarket weather market close)."
        ),
    )
    args = p.parse_args()
    configure_logging(args.verbose)

    if not args.no_migrate:
        init_schema_and_seed()

    targets = [
        args.date + dt.timedelta(days=i) for i in range(args.days_ahead + 1)
    ]
    models = tuple(m.strip() for m in args.models.split(",") if m.strip())
    stations = parse_stations(args.station)

    as_of: dt.datetime | None = None
    if args.as_of is not None:
        if not (0 <= args.cutoff_hour <= 23):
            raise SystemExit(
                f"--cutoff-hour must be in [0, 23], got {args.cutoff_hour}"
            )
        as_of = dt.datetime(
            args.as_of.year, args.as_of.month, args.as_of.day,
            args.cutoff_hour, 0, 0, tzinfo=dt.timezone.utc,
        )
        log.info("Historical replay anchored at as_of=%s", as_of.isoformat())

    legacy_models = tuple(m for m in models if m in {MODEL_M0, MODEL_M1})
    total_pred = 0
    total_probs = 0
    if legacy_models:
        counts = run_predictions(
            stations, targets, models=legacy_models, as_of=as_of
        )
        total_pred += counts["predictions"]
        total_probs += counts["bucket_probs"]
    if MODEL_M2 in models:
        counts = run_m2_predictions(stations, targets, as_of=as_of)
        total_pred += counts["predictions"]
        total_probs += counts["bucket_probs"]

    mode = f"as_of={as_of.isoformat()}" if as_of else "live"
    print(
        f"Wrote predictions={total_pred} bucket_probs={total_probs} "
        f"models={list(models)} dates=[{targets[0]}..{targets[-1]}] {mode}"
    )


if __name__ == "__main__":
    main()
