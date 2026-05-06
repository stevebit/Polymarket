"""Cancel a single order by id (requires --confirm YES)."""

from __future__ import annotations

import argparse
import json

from py_clob_client_v2.clob_types import OrderPayload

from polymarket_manual.clients import make_trading_client
from polymarket_manual.config import load_settings

_CONFIRM_TOKEN = "YES"


def main() -> None:
    parser = argparse.ArgumentParser(description="Cancel one open order.")
    parser.add_argument("--order-id", required=True)
    parser.add_argument(
        "--confirm",
        required=True,
        help=f'Must be "{_CONFIRM_TOKEN}" to cancel.',
    )
    args = parser.parse_args()

    if args.confirm != _CONFIRM_TOKEN:
        raise SystemExit(f'Pass --confirm {_CONFIRM_TOKEN} after verifying the order id.')

    settings = load_settings()
    client = make_trading_client(settings)
    resp = client.cancel_order(OrderPayload(orderID=args.order_id))
    print(json.dumps(resp, indent=2, default=str))


if __name__ == "__main__":
    main()
