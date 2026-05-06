"""
Post a single GTC limit order after explicit confirmation.

Dry-run prints the signed payload but does not post.
"""

from __future__ import annotations

import argparse
import json

from py_clob_client_v2.clob_types import OrderArgsV2, OrderType
from py_clob_client_v2.order_builder.constants import BUY, SELL

from polymarket_manual.clients import make_trading_client
from polymarket_manual.config import load_settings

_CONFIRM_TOKEN = "YES"


def main() -> None:
    parser = argparse.ArgumentParser(description="Place one GTC limit order (manual gate).")
    parser.add_argument("--token-id", required=True)
    parser.add_argument("--price", type=float, required=True, help="0.00 - 1.00")
    parser.add_argument("--size", type=float, required=True, help="Shares (outcome tokens)")
    parser.add_argument("--side", choices=("buy", "sell"), required=True)
    parser.add_argument(
        "--confirm",
        help=f'Must be "{_CONFIRM_TOKEN}" to post (after you read the summary).',
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and print the signed order JSON; do not call post_order.",
    )
    args = parser.parse_args()

    side = BUY if args.side == "buy" else SELL
    order = OrderArgsV2(token_id=args.token_id, price=args.price, size=args.size, side=side)

    settings = load_settings()
    client = make_trading_client(settings)
    signed = client.create_order(order)

    summary = {
        "token_id": args.token_id,
        "price": args.price,
        "size": args.size,
        "side": args.side,
        "order_type": "GTC",
        "dry_run": args.dry_run,
    }
    print("Summary:", json.dumps(summary, indent=2))
    print("Signed order (truncated JSON):", json.dumps(signed, default=str)[:4000])

    if args.dry_run:
        print("Dry-run: not posting.")
        return

    if args.confirm != _CONFIRM_TOKEN:
        raise SystemExit(
            f'Refusing to post without --confirm {_CONFIRM_TOKEN}. '
            "Re-run the same flags with --confirm YES after verifying the summary."
        )

    resp = client.post_order(signed, OrderType.GTC)
    print("post_order response:", json.dumps(resp, indent=2, default=str))


if __name__ == "__main__":
    main()
