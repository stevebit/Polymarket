"""
Post a market order (BUY: spend up to $amount USDC; SELL: share amount).

Uses FOK by default (fill-or-kill). Manual confirmation required to post.
"""

from __future__ import annotations

import argparse
import json

from py_clob_client_v2.clob_types import MarketOrderArgsV2, OrderType
from py_clob_client_v2.order_builder.constants import BUY, SELL

from polymarket_manual.clients import make_trading_client
from polymarket_manual.config import load_settings

_CONFIRM_TOKEN = "YES"


def main() -> None:
    parser = argparse.ArgumentParser(description="Place one market order (FOK default for BUY $).")
    parser.add_argument("--token-id", required=True)
    parser.add_argument(
        "--amount",
        type=float,
        required=True,
        help="For side=buy: USDC notional to spend. For side=sell: shares to sell.",
    )
    parser.add_argument("--side", choices=("buy", "sell"), required=True)
    parser.add_argument(
        "--order-type",
        choices=("FOK", "FAK"),
        default="FOK",
        help="FOK = fill-or-kill (default). FAK = fill-and-kill partial.",
    )
    parser.add_argument(
        "--confirm",
        help=f'Must be "{_CONFIRM_TOKEN}" to post.',
    )
    parser.add_argument("--dry-run", action="store_true", help="Build signed order only; do not post.")
    args = parser.parse_args()

    ot = OrderType.FOK if args.order_type == "FOK" else OrderType.FAK
    side = BUY if args.side == "buy" else SELL

    mo = MarketOrderArgsV2(
        token_id=args.token_id,
        amount=args.amount,
        side=side,
        order_type=ot,
    )

    settings = load_settings()
    client = make_trading_client(settings)

    summary = {
        "token_id": args.token_id,
        "amount": args.amount,
        "side": args.side,
        "order_type": args.order_type,
        "dry_run": args.dry_run,
    }
    print("Summary:", json.dumps(summary, indent=2))

    if args.dry_run:
        signed = client.create_market_order(mo)
        print("Signed order (truncated):", json.dumps(signed, default=str)[:4000])
        print("Dry-run: not posting.")
        return

    if args.confirm != _CONFIRM_TOKEN:
        raise SystemExit(
            f'Refusing to post without --confirm {_CONFIRM_TOKEN}. '
            "Review the summary, then re-run with --confirm YES."
        )

    resp = client.create_and_post_market_order(mo, order_type=ot)
    print("create_and_post_market_order response:", json.dumps(resp, indent=2, default=str))


if __name__ == "__main__":
    main()
