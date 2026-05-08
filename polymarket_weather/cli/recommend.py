"""Daily recommendations report (read-only).

Builds the per-event taker / maker / neg-risk recommendation table from the
latest M2 (or M1 fallback) bucket probabilities and the latest market
snapshots, and writes a markdown report to ``reports/recommendations_<ts>.md``.

This CLI **never** signs or places orders. It is the human-review gate for
Phase 3 and the input to the Phase 4 backtest replay loop.
"""

from __future__ import annotations

import argparse

from ..models.baseline import MODEL_M1
from ..models.m2_postprocessed_ensemble import MODEL_M2
from ..recommend import (
    build_recommendations,
    render_recommendations_markdown,
    write_recommendations_report,
)
from ..strategy.edge import FeeSchedule
from ..strategy.sizing import DEFAULT_MIN_EDGE_PER_DOLLAR, CapsConfig
from ._common import add_common_args, configure_logging, parse_stations


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    add_common_args(p, with_date=False)
    p.add_argument("--days-ahead", type=int, default=7)
    p.add_argument(
        "--bankroll", type=float, default=500.0, help="Total bankroll in USD."
    )
    p.add_argument(
        "--per-bucket-cap", type=float, default=5.0,
        help="Maximum dollar notional per bucket per side.",
    )
    p.add_argument(
        "--per-event-cap", type=float, default=20.0,
        help="Maximum dollar notional per event across all buckets/sides.",
    )
    p.add_argument(
        "--per-day-cap", type=float, default=100.0,
        help="Maximum dollar notional per day across all events.",
    )
    p.add_argument(
        "--per-portfolio-cap", type=float, default=500.0,
        help="Maximum total open dollar notional in the portfolio.",
    )
    p.add_argument(
        "--kelly-fraction", type=float, default=0.25,
        help="Fractional-Kelly multiplier (1.0 = full Kelly).",
    )
    p.add_argument(
        "--min-edge-cents", type=float,
        default=DEFAULT_MIN_EDGE_PER_DOLLAR * 100.0,
        help="Minimum edge per $1 notional, in cents.",
    )
    p.add_argument(
        "--target-spread", type=float, default=0.04,
        help="Maker quote half-width target (full spread).",
    )
    p.add_argument(
        "--primary-model", default=MODEL_M2, help="Preferred model id."
    )
    p.add_argument(
        "--fallback-model", default=MODEL_M1,
        help="Fallback model id when primary has no probs for an event.",
    )
    p.add_argument(
        "--print-only", action="store_true",
        help="Print to stdout without writing a markdown file.",
    )
    args = p.parse_args()
    configure_logging(args.verbose)

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

    recs = build_recommendations(
        station_slugs=parse_stations(args.station),
        days_ahead=args.days_ahead,
        primary_model=args.primary_model,
        fallback_model=args.fallback_model,
        caps=caps,
        fees=fees,
        target_spread=args.target_spread,
    )
    if not recs:
        print("No active events / recommendations.")
        return

    if args.print_only:
        print(render_recommendations_markdown(
            recs, caps=caps, fees=fees, target_spread=args.target_spread
        ))
    else:
        path = write_recommendations_report(
            recs, caps=caps, fees=fees, target_spread=args.target_spread
        )
        n_taker = sum(
            1 for r in recs for b in r.buckets if b.best_taker is not None
        )
        n_maker_buy = sum(
            1 for r in recs for b in r.buckets if b.maker_buy is not None
        )
        n_maker_sell = sum(
            1 for r in recs for b in r.buckets if b.maker_sell is not None
        )
        flagged = sum(1 for r in recs if r.flagged_arb)
        total_ev = sum(r.total_taker_ev_usd for r in recs)
        print(
            f"Wrote {path}\n"
            f"  events={len(recs)} taker_ideas={n_taker} "
            f"maker_buys={n_maker_buy} maker_sells={n_maker_sell} "
            f"arb_flagged={flagged} total_taker_ev=${total_ev:+.2f}"
        )


if __name__ == "__main__":
    main()
