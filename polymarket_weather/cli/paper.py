"""Paper-trading CLI: submit, settle, summarise.

Subcommands:

* ``submit``    — runs the recommend pipeline and persists every
                  recommended order into ``paper_trades``. Idempotent enough
                  for a 15-minute scheduler: re-submitting will just add new
                  rows reflecting the current snapshot's recommendations.
* ``settle``    — for every unsettled paper trade whose event has resolved
                  (and is at least ``--older-than-days`` old), look up the
                  WU/NOAA observation, mark realised PnL, and stamp settled_at.
* ``summary``   — render a markdown summary of the last N days of paper
                  trading: PnL by station, by side, vs predicted EV.
"""

from __future__ import annotations

import argparse
import datetime as dt

from ..db import init_schema_and_seed
from ..paper import (
    render_paper_summary_markdown,
    run_paper_session,
    settle_paper_trades,
)
from ..strategy.sizing import (
    DEFAULT_MIN_EDGE_PER_DOLLAR,
    CapsConfig,
)
from ._common import add_common_args, configure_logging, parse_stations


def _add_caps_args(p: argparse.ArgumentParser) -> None:
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


def _build_caps(args: argparse.Namespace) -> CapsConfig:
    return CapsConfig(
        bankroll_usd=args.bankroll,
        per_bucket_usd=args.per_bucket_cap,
        per_event_usd=args.per_event_cap,
        per_day_usd=args.per_day_cap,
        per_portfolio_usd=args.per_portfolio_cap,
        kelly_fraction=args.kelly_fraction,
        min_edge_per_dollar=args.min_edge_cents / 100.0,
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    add_common_args(p, with_date=False)
    p.add_argument("--no-migrate", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    submit_p = sub.add_parser("submit", help="Persist recommended orders into paper_trades.")
    submit_p.add_argument("--days-ahead", type=int, default=7)
    _add_caps_args(submit_p)
    submit_p.add_argument(
        "--no-taker", action="store_true", help="Skip persisting taker orders."
    )
    submit_p.add_argument(
        "--no-maker", action="store_true", help="Skip persisting maker quotes."
    )

    settle_p = sub.add_parser(
        "settle", help="Mark resolved paper trades with realised PnL."
    )
    settle_p.add_argument(
        "--older-than-days", type=int, default=1,
        help="Only settle trades whose target_date is at least N days old.",
    )

    summary_p = sub.add_parser("summary", help="Print a markdown summary.")
    summary_p.add_argument("--days", type=int, default=30)

    args = p.parse_args()
    configure_logging(args.verbose)
    if not args.no_migrate:
        init_schema_and_seed()

    if args.cmd == "submit":
        caps = _build_caps(args)
        n = run_paper_session(
            station_slugs=parse_stations(args.station),
            days_ahead=args.days_ahead,
            caps=caps,
            persist_taker=not args.no_taker,
            persist_maker=not args.no_maker,
        )
        print(f"Submitted {n} paper orders.")
    elif args.cmd == "settle":
        result = settle_paper_trades(older_than_days=args.older_than_days)
        rl = (
            f"{result.realised_log_loss:.4f}"
            if result.realised_log_loss is not None
            else "—"
        )
        print(
            f"Settled {result.n_settled} trades; "
            f"skipped (unresolved) {result.n_skipped_unresolved}; "
            f"realised PnL=${result.pnl_total_usd:+.2f}; "
            f"realised log_loss={rl}"
        )
    elif args.cmd == "summary":
        print(render_paper_summary_markdown(days=args.days))


if __name__ == "__main__":
    main()
