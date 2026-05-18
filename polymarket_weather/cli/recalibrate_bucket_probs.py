"""Apply the latest isotonic fit in-place to existing ``bucket_probs`` rows.

Background
----------
Phase 6 introduced ``isotonic_calibration`` (migration 006) and wired the
``apply_isotonic`` step into both the M0/M1 driver
(``polymarket_weather.models.baseline.run_predictions``) and the M2
driver (``polymarket_weather.models.m2_postprocessed_ensemble.run_m2_predictions``).
Live predict ticks therefore persist calibrated probabilities by default.

The historical ``predict_history`` driver, however, can take many hours to
re-run over a full backtest window. To pick up an isotonic fit retroactively
without re-running the prediction stack, this CLI:

1. Reads the latest fit per ``--model`` from ``isotonic_calibration``.
2. SELECTs every ``bucket_probs`` row whose ``run_time`` falls inside
   ``[--start, --end]`` (and optionally only for events whose
   ``target_date`` is on or before today).
3. Groups by ``(model_id, event_slug, run_time)`` so the per-row apply
   step renormalises across the full bucket vector (as it does at
   persist time).
4. Bulk-upserts the calibrated probabilities back into ``bucket_probs``.

The fit-set events are by definition in-sample relative to the isotonic
fit, so any post-fix improvement measured on the same calibrate window is
optimistically biased. That bias is documented in ``docs/REVIEW_2026_05_12.md``.

Usage::

    python -m polymarket_weather.cli.recalibrate_bucket_probs \
        --model m2_postprocessed_ens \
        --start 2025-04-20 --end 2026-05-17
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from ..db import init_schema_and_seed, with_conn
from ..models.isotonic import apply_isotonic, latest_isotonic_fit
from ._common import configure_logging, parse_cli_date

log = logging.getLogger(__name__)


UPSERT_SQL = """
INSERT INTO bucket_probs (model_id, event_slug, bucket_label, run_time, prob)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (model_id, event_slug, bucket_label, run_time) DO UPDATE SET
    prob = EXCLUDED.prob
"""


def _recalibrate_for(model_id: str, start: dt.date, end: dt.date) -> dict[str, int]:
    fit = latest_isotonic_fit(model_id)
    if fit is None:
        log.warning("[%s] no isotonic fit found — skipping", model_id)
        return {"events": 0, "rows": 0}

    n_events = 0
    n_rows = 0
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT bp.event_slug,
                   bp.run_time,
                   bp.bucket_label,
                   bp.prob::float8
              FROM bucket_probs bp
             WHERE bp.model_id = %s
               AND bp.run_time::date >= %s
               AND bp.run_time::date <= %s
             ORDER BY bp.event_slug, bp.run_time, bp.bucket_label
            """,
            (model_id, start, end),
        )
        rows = cur.fetchall()
        log.info("[%s] fetched %d rows for [%s..%s]", model_id, len(rows), start, end)

        # Group by (event_slug, run_time) — each group is one full bucket
        # distribution that must be renormalised together.
        current_key: tuple[str, dt.datetime] | None = None
        current_probs: dict[str, float] = {}
        params: list[tuple] = []

        def flush(key: tuple[str, dt.datetime] | None, probs: dict[str, float]):
            nonlocal n_events, n_rows
            if not key or not probs:
                return
            cal = apply_isotonic(fit, probs)
            event_slug, run_time = key
            for label, p in cal.items():
                params.append((model_id, event_slug, label, run_time, float(p)))
                n_rows += 1
            n_events += 1

        for event_slug, run_time, label, prob in rows:
            key = (event_slug, run_time)
            if key != current_key:
                flush(current_key, current_probs)
                current_key = key
                current_probs = {}
            current_probs[label] = float(prob)
        flush(current_key, current_probs)

        if params:
            log.info("[%s] writing %d rows back...", model_id, len(params))
            cur.executemany(UPSERT_SQL, params)

    log.info("[%s] events=%d rows=%d", model_id, n_events, n_rows)
    return {"events": n_events, "rows": n_rows}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--model",
        required=True,
        help="Comma-separated model_id list to recalibrate.",
    )
    p.add_argument(
        "--start",
        type=parse_cli_date,
        required=True,
        help="Inclusive start date (UTC) for run_time::date.",
    )
    p.add_argument(
        "--end",
        type=parse_cli_date,
        default=dt.datetime.now(dt.timezone.utc).date(),
        help="Inclusive end date (UTC) for run_time::date. Default today UTC.",
    )
    p.add_argument("--no-migrate", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    configure_logging(args.verbose)

    if not args.no_migrate:
        init_schema_and_seed()

    if args.end < args.start:
        raise SystemExit(f"--end {args.end} is before --start {args.start}")

    model_ids = [m.strip() for m in args.model.split(",") if m.strip()]
    if not model_ids:
        print("--model parsed to an empty list", file=sys.stderr)
        sys.exit(2)

    total_events = 0
    total_rows = 0
    for model_id in model_ids:
        counts = _recalibrate_for(model_id, args.start, args.end)
        total_events += counts["events"]
        total_rows += counts["rows"]
        print(
            f"[{model_id}] events={counts['events']} rows={counts['rows']}"
        )

    print(
        f"recalibrate_bucket_probs done: total_events={total_events} "
        f"total_rows={total_rows} window=[{args.start}..{args.end}]"
    )


if __name__ == "__main__":
    main()
