"""Single-shot or long-running orchestrator entrypoint.

Default: one-shot single tick (matches Task Scheduler / cron usage).
Add ``--loop`` to keep ticking every ``--interval-seconds``.

Mode selection:

* Default ``--mode paper``: writes to ``paper_trades`` only; no signed flow.
* ``--mode live``: requires ``WEATHER_AUTOMATION_ENABLED=1`` and unset
  ``WEATHER_KILL_SWITCH``. Falls back to paper with a loud warning if the
  envs aren't set, so accidental ``live`` invocation never trades.

The orchestrator produces structured logs in ``logs/orch_<date>.jsonl`` and
refreshes ``daily_pnl`` per tick.
"""

from __future__ import annotations

import argparse

from ..automation.orchestrator import run_loop, run_tick
from ..db import init_schema_and_seed
from ..strategy.sizing import (
    DEFAULT_MIN_EDGE_PER_DOLLAR,
    CapsConfig,
)
from ._common import add_common_args, configure_logging, parse_stations


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    add_common_args(p, with_date=False)
    p.add_argument(
        "--mode", choices=("paper", "live"), default="paper",
        help="Live signed flow requires WEATHER_AUTOMATION_ENABLED=1.",
    )
    p.add_argument(
        "--days-ahead", type=int, default=7,
        help="Active window: target_date <= today + N.",
    )
    p.add_argument(
        "--no-ingest", action="store_true",
        help="Skip forecast/observation ingest this tick.",
    )
    p.add_argument(
        "--no-snapshot", action="store_true",
        help="Skip event discovery + book snapshot this tick.",
    )
    p.add_argument(
        "--loop", action="store_true",
        help="Run forever (else single tick).",
    )
    p.add_argument(
        "--interval-seconds", type=int, default=900,
        help="Sleep between ticks when --loop is set (default 15 min).",
    )
    p.add_argument(
        "--max-iterations", type=int, default=None,
        help="Cap loop iterations (handy for testing).",
    )
    p.add_argument("--no-migrate", action="store_true")
    # Caps
    p.add_argument("--bankroll", type=float, default=500.0)
    p.add_argument("--per-bucket-cap", type=float, default=5.0)
    p.add_argument("--per-event-cap", type=float, default=20.0)
    p.add_argument("--per-day-cap", type=float, default=100.0)
    p.add_argument("--per-portfolio-cap", type=float, default=500.0)
    p.add_argument("--kelly-fraction", type=float, default=0.25)
    p.add_argument(
        "--min-edge-cents", type=float,
        default=DEFAULT_MIN_EDGE_PER_DOLLAR * 100.0,
    )

    args = p.parse_args()
    configure_logging(args.verbose)

    if not args.no_migrate:
        init_schema_and_seed()

    caps = CapsConfig(
        bankroll_usd=args.bankroll,
        per_bucket_usd=args.per_bucket_cap,
        per_event_usd=args.per_event_cap,
        per_day_usd=args.per_day_cap,
        per_portfolio_usd=args.per_portfolio_cap,
        kelly_fraction=args.kelly_fraction,
        min_edge_per_dollar=args.min_edge_cents / 100.0,
    )

    common_kwargs = dict(
        mode=args.mode,
        station_slugs=parse_stations(args.station),
        days_ahead=args.days_ahead,
        caps=caps,
        do_ingest=not args.no_ingest,
        do_snapshot=not args.no_snapshot,
    )
    if args.loop:
        run_loop(
            interval_seconds=args.interval_seconds,
            max_iterations=args.max_iterations,
            **common_kwargs,
        )
        return

    result = run_tick(**common_kwargs)
    print(
        f"mode={result.mode} fallback_paper={result.fallback_to_paper} "
        f"snapshots={result.snapshots_taken} "
        f"preds={result.predictions_written} "
        f"bucket_probs={result.bucket_probs_written} "
        f"paper_orders={result.paper_orders} "
        f"placed={result.placed_orders} "
        f"cap_breaches={result.cap_breaches} "
        f"errors={len(result.errors)}"
    )
    for err in result.errors:
        print(f"  ERROR: {err}")


if __name__ == "__main__":
    main()
