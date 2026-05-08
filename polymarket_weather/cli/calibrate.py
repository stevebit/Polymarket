"""Calibration / backtest. Writes a markdown report under ``reports/`` and a
row to ``calibration_runs``."""

from __future__ import annotations

import argparse

from ..calibration import MODEL_MARKET_MID, run_calibration
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
        "--include-market",
        action="store_true",
        help=(
            f"Also score the market-implied baseline (model_id={MODEL_MARKET_MID!r}); "
            "uses the latest pm_market_snapshots.mid per bucket as the probability "
            "vector. Apples-to-apples comparison vs M0/M1/M2."
        ),
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
    model_ids = [m.strip() for m in args.model.split(",") if m.strip()]
    if args.include_market and MODEL_MARKET_MID not in model_ids:
        model_ids.append(MODEL_MARKET_MID)

    results = []
    for model_id in model_ids:
        result = run_calibration(
            model_id,
            station_slugs=stations,
            lookback_days=args.lookback_days,
        )
        results.append(result)
        print(
            f"[{model_id}] run_id={result.run_id} sample_n={result.sample_n} "
            f"log_loss={result.log_loss} brier={result.brier}"
        )
        print(f"  report: {result.report_path}")

    if len(results) > 1:
        print()
        print("Comparison (lower is better):")
        print(f"{'model_id':<35} {'n':>6} {'log_loss':>10} {'brier':>10}")
        for r in results:
            ll = "—" if r.log_loss is None else f"{r.log_loss:.4f}"
            br = "—" if r.brier is None else f"{r.brier:.4f}"
            print(f"{r.model_id:<35} {r.sample_n:>6} {ll:>10} {br:>10}")


if __name__ == "__main__":
    main()
