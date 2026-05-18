"""Drive ``predict --as-of`` for every day in a backtest window.

Plan Phase 4: after Open-Meteo historical forecasts are backfilled
(``scripts/backfill_open_meteo_history.py``) and EMOS is refit
(``python -m polymarket_weather.cli.fit_postprocess``), this driver loops
day-by-day calling the same ``predict_m0_for / predict_m1_for /
predict_m2_for`` machinery the live tick uses, with ``as_of = <day> T
cutoff_hour UTC``. The result is a set of ``bucket_probs`` rows whose
``run_time`` reflects the model state we *would have* had on each day —
which is what the calibration SQL (Phase 1c DISTINCT-ON) and the
``backtest`` replay both expect.

Run roughly:

    python -m polymarket_weather.cli.predict_history \\
        --start 2025-11-01 --end 2026-05-12 --book-alignment utc-noon
    python -m polymarket_weather.cli.predict_history \\
        --start 2025-11-01 --end 2026-05-12 --book-alignment utc-midnight

Use ``utc-midnight`` (00:00 UTC ``run_time``) when replaying **intraday**
CLOB snapshots so ``run_time <= snapshot_at`` holds for morning books.
``utc-noon`` (12:00 UTC) matches the legacy daily anchor. With ``utc-noon``,
pair backtests using ``--min-snapshot-utc-hour 12`` or accept only afternoon
books.

This is the long-running driver behind the Phase 6 like-for-like
calibration. Each (date, station, model) triple is cheap; the wall-clock
cost is dominated by ``predict_m2_for``'s DB round trips.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import time

import psycopg
from psycopg_pool import PoolTimeout

from ..db import close_pool, init_schema_and_seed
from ..models.baseline import MODEL_M0, MODEL_M1, run_predictions
from ..models.m2_postprocessed_ensemble import MODEL_M2, run_m2_predictions
from ._common import (
    add_common_args,
    configure_logging,
    parse_cli_date,
    parse_stations,
)

log = logging.getLogger(__name__)

_DB_RETRYABLE = (psycopg.OperationalError, PoolTimeout)
_DB_ATTEMPTS = 8


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    add_common_args(p, with_date=False)
    p.add_argument(
        "--start",
        type=parse_cli_date,
        required=True,
        help="Inclusive start date: YYYY-MM-DD or today/yesterday (UTC).",
    )
    p.add_argument(
        "--end",
        type=parse_cli_date,
        default=dt.datetime.now(dt.timezone.utc).date(),
        help="Inclusive end date: YYYY-MM-DD or today/yesterday (UTC). Default today UTC.",
    )
    p.add_argument(
        "--cutoff-hour",
        type=int,
        default=12,
        help=(
            "UTC hour for the as-of anchor when --book-alignment from-cutoff-hour "
            "(default). Ignored when alignment is utc-midnight or utc-noon."
        ),
    )
    p.add_argument(
        "--book-alignment",
        choices=("from-cutoff-hour", "utc-midnight", "utc-noon"),
        default="from-cutoff-hour",
        help=(
            "utc-midnight → cutoff 0 (intraday books). utc-noon → cutoff 12. "
            "from-cutoff-hour → use --cutoff-hour only (default)."
        ),
    )
    p.add_argument(
        "--days-ahead", type=int, default=0,
        help=(
            "For each anchor day, also predict targets [as_of, as_of + N]. "
            "0 (default) replays only the same-day event."
        ),
    )
    p.add_argument(
        "--models",
        default=f"{MODEL_M0},{MODEL_M1},{MODEL_M2}",
        help="Comma-separated model_id list to replay.",
    )
    p.add_argument("--no-migrate", action="store_true")
    args = p.parse_args()
    configure_logging(args.verbose)

    if not args.no_migrate:
        init_schema_and_seed()

    if args.end < args.start:
        raise SystemExit(f"--end {args.end} is before --start {args.start}")

    if args.book_alignment == "utc-midnight":
        eff_cutoff = 0
    elif args.book_alignment == "utc-noon":
        eff_cutoff = 12
    else:
        eff_cutoff = args.cutoff_hour

    if not (0 <= eff_cutoff <= 23):
        raise SystemExit(
            f"effective UTC cutoff hour must be in [0, 23], got {eff_cutoff}"
        )
    if args.book_alignment == "from-cutoff-hour" and not (
        0 <= args.cutoff_hour <= 23
    ):
        raise SystemExit(
            f"--cutoff-hour must be in [0, 23], got {args.cutoff_hour}"
        )

    stations = parse_stations(args.station)
    models = tuple(m.strip() for m in args.models.split(",") if m.strip())
    legacy_models = tuple(m for m in models if m in {MODEL_M0, MODEL_M1})
    do_m2 = MODEL_M2 in models

    total_pred = 0
    total_probs = 0
    one_day = dt.timedelta(days=1)

    day = args.start
    while day <= args.end:
        as_of = dt.datetime(
            day.year, day.month, day.day,
            eff_cutoff, 0, 0, tzinfo=dt.timezone.utc,
        )
        targets = [day + dt.timedelta(days=i) for i in range(args.days_ahead + 1)]

        d_pred = 0
        d_probs = 0
        for attempt in range(_DB_ATTEMPTS):
            try:
                if legacy_models:
                    counts = run_predictions(
                        stations, targets, models=legacy_models, as_of=as_of
                    )
                    d_pred += counts["predictions"]
                    d_probs += counts["bucket_probs"]
                if do_m2:
                    counts = run_m2_predictions(stations, targets, as_of=as_of)
                    d_pred += counts["predictions"]
                    d_probs += counts["bucket_probs"]
                break
            except _DB_RETRYABLE as exc:
                if attempt == _DB_ATTEMPTS - 1:
                    raise
                d_pred = 0
                d_probs = 0
                delay = min(180.0, 10.0 * (2**attempt))
                log.warning(
                    "transient DB error on %s (%d/%d): %s — "
                    "closing pool, retrying in %.0fs",
                    as_of.date(),
                    attempt + 1,
                    _DB_ATTEMPTS,
                    exc,
                    delay,
                )
                close_pool()
                time.sleep(delay)
        total_pred += d_pred
        total_probs += d_probs
        log.info(
            "as_of=%s done: cumulative predictions=%d bucket_probs=%d",
            as_of.isoformat(), total_pred, total_probs,
        )
        day += one_day

    print(
        f"predict_history done: predictions={total_pred} bucket_probs={total_probs} "
        f"window=[{args.start}..{args.end}] "
        f"book_alignment={args.book_alignment} utc_cutoff_hour={eff_cutoff}"
    )


if __name__ == "__main__":
    main()
