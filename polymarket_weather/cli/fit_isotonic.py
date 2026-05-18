"""Fit and persist a per-model isotonic recalibration of bucket probabilities.

Phase 6 (plan): run after the like-for-like calibration shows a model has
consistent off-diagonal reliability bins. Stores one row per fit in
``isotonic_calibration``; the latest ``fit_at`` per model_id is consumed
by ``apply_isotonic`` at predict time.

The decision rule (review §6) is checked by ``isotonic_recommended``: at
least 3 reliability bins off-diagonal in the **same** direction. Pass
``--force`` to skip the check and fit anyway.

Examples::

    python -m polymarket_weather.cli.fit_isotonic --model m2_postprocessed_ens
    python -m polymarket_weather.cli.fit_isotonic --model m1_ensemble_gaussian --force
"""

from __future__ import annotations

import argparse
import logging
import sys

import numpy as np

from ..db import init_schema_and_seed, with_conn
from ..models.isotonic import (
    _fetch_flattened_pairs,
    fit_isotonic,
    isotonic_recommended,
)
from ._common import configure_logging

log = logging.getLogger(__name__)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--model",
        required=True,
        help="Comma-separated model_id list to fit (e.g. m1_ensemble_gaussian,m2_postprocessed_ens).",
    )
    p.add_argument("--lookback-days", type=int, default=90)
    p.add_argument(
        "--force",
        action="store_true",
        help="Skip the reliability-bin check and fit anyway.",
    )
    p.add_argument("--no-migrate", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    configure_logging(args.verbose)

    if not args.no_migrate:
        init_schema_and_seed()

    model_ids = [m.strip() for m in args.model.split(",") if m.strip()]
    if not model_ids:
        print("--model parsed to an empty list", file=sys.stderr)
        sys.exit(2)

    any_failed = False
    for model_id in model_ids:
        with with_conn() as conn, conn.cursor() as cur:
            xs, ys = _fetch_flattened_pairs(cur, model_id, args.lookback_days)

        if xs.size == 0:
            print(f"[{model_id}] no resolved data — nothing to fit.")
            any_failed = True
            continue

        if not args.force and not isotonic_recommended(xs, ys):
            print(
                f"[{model_id}] reliability bins do NOT show consistent off-diagonal "
                "miscalibration; skipping fit. Pass --force to fit anyway."
            )
            continue

        fit = fit_isotonic(model_id, lookback_days=args.lookback_days)
        if fit is None:
            print(f"[{model_id}] fit_isotonic returned None — too few pairs.")
            any_failed = True
            continue
        print(
            f"[{fit.model_id}] fit n_train={fit.n_train} knots={len(fit.x_knots)} "
            f"train_brier={fit.train_brier:.4f} train_logloss={fit.train_logloss:.4f}"
        )

    if any_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
