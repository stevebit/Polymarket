"""Replay historical ``pm_market_snapshots`` and report PnL after fees.

Realised PnL is settled against ``observations`` (preferring
``wunderground:historical``). The model's bucket_probs are read from history
with ``run_time <= snapshot_at`` to avoid look-ahead.
"""

from __future__ import annotations

import argparse
import datetime as dt

from pathlib import Path

from ..backtest import (
    backtest_result_to_dict,
    export_backtest_json,
    render_backtest_markdown,
    replay_backtest,
    write_backtest_report,
)
from ..models.baseline import MODEL_M1
from ..models.m2_postprocessed_ensemble import MODEL_M2
from ..strategy.edge import FeeSchedule
from ..strategy.sizing import (
    DEFAULT_MIN_EDGE_PER_DOLLAR,
    CapsConfig,
)
from ._common import add_common_args, configure_logging, parse_stations


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    add_common_args(p, with_date=False)
    p.add_argument(
        "--start", type=lambda s: dt.date.fromisoformat(s), required=True
    )
    p.add_argument(
        "--end", type=lambda s: dt.date.fromisoformat(s),
        default=dt.date.today(),
    )
    p.add_argument(
        "--model", default=MODEL_M2,
        help=f"Bucket-prob model_id (e.g. {MODEL_M1}, {MODEL_M2}).",
    )
    p.add_argument(
        "--strategy", choices=("taker", "maker", "both"), default="both"
    )
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
    p.add_argument(
        "--target-spread", type=float, default=0.04,
    )
    p.add_argument(
        "--print-only", action="store_true",
        help="Print to stdout without writing a markdown file.",
    )
    p.add_argument(
        "--take-every-n-snapshots",
        type=int,
        default=1,
        help="Replay every Nth snapshot in time order (1 = all). Reduces runtime "
        "and approximates coarser book updates for maker stress tests.",
    )
    p.add_argument(
        "--export-json",
        type=Path,
        default=None,
        help="Also write a JSON bundle for backtest_dashboard / external analysis.",
    )
    p.add_argument(
        "--slippage-per-share",
        type=float,
        default=0.005,
        help=(
            "Per-share slippage haircut subtracted from taker EV before "
            "sizing (review §5.6). 0 disables. Default 0.5 cents."
        ),
    )
    p.add_argument(
        "--min-snapshot-utc-hour",
        type=int,
        default=None,
        metavar="H",
        help=(
            "Only load snapshots whose clock hour in UTC is >= H (0..23). "
            "Use with model runs anchored at UTC noon, e.g. --book-alignment "
            "utc-noon on predict_history."
        ),
    )
    args = p.parse_args()
    configure_logging(args.verbose)

    if args.min_snapshot_utc_hour is not None and not (
        0 <= args.min_snapshot_utc_hour <= 23
    ):
        raise SystemExit(
            "--min-snapshot-utc-hour must be in [0, 23], "
            f"got {args.min_snapshot_utc_hour}"
        )

    caps = CapsConfig(
        bankroll_usd=args.bankroll,
        per_bucket_usd=args.per_bucket_cap,
        per_event_usd=args.per_event_cap,
        per_day_usd=args.per_day_cap,
        per_portfolio_usd=args.per_portfolio_cap,
        kelly_fraction=args.kelly_fraction,
        min_edge_per_dollar=args.min_edge_cents / 100.0,
    )

    fees = FeeSchedule()
    result = replay_backtest(
        model_id=args.model,
        station_slugs=parse_stations(args.station),
        start=args.start,
        end=args.end,
        caps=caps,
        fees=fees,
        strategy=args.strategy,
        target_spread=args.target_spread,
        take_every_n_snapshots=args.take_every_n_snapshots,
        slippage_per_share=args.slippage_per_share,
        min_snapshot_utc_hour=args.min_snapshot_utc_hour,
    )

    if args.export_json:
        payload = backtest_result_to_dict(
            result,
            model_id=args.model,
            start=args.start,
            end=args.end,
            strategy=args.strategy,
            caps=caps,
            fees=fees,
        )
        export_backtest_json(args.export_json, payload)
        print(f"Wrote JSON {args.export_json}")

    if args.print_only:
        print(render_backtest_markdown(
            result,
            model_id=args.model,
            start=args.start,
            end=args.end,
            strategy=args.strategy,
            caps=caps,
        ))
        return

    path = write_backtest_report(
        result,
        model_id=args.model,
        start=args.start,
        end=args.end,
        strategy=args.strategy,
        caps=caps,
    )
    print(
        f"Wrote {path}\n"
        f"  taker_pnl=${result.pnl_taker_usd:+.2f} "
        f"maker_pnl=${result.pnl_maker_usd:+.2f} "
        f"net=${result.pnl_taker_usd + result.pnl_maker_usd:+.2f} "
        f"taker_fills={len(result.fills_taker)} "
        f"maker_fills={len(result.fills_maker)} "
        f"events_resolved={result.n_events_resolved}"
    )


if __name__ == "__main__":
    main()
