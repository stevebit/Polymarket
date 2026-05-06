"""Print midpoint, best bid/ask, and a short book snapshot for one token."""

from __future__ import annotations

import argparse
import json
import sys

from py_clob_client_v2.exceptions import PolyApiException

from polymarket_manual.clients import make_readonly_client
from polymarket_manual.config import load_settings


def _top_levels(book_side, n: int = 5):
    if book_side is None:
        return []
    raw = getattr(book_side, "levels", None) or getattr(book_side, "bids", None)
    if raw is None:
        return []
    return raw[:n]


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only order book snapshot.")
    parser.add_argument("--token-id", required=True, help="CLOB outcome token id")
    args = parser.parse_args()

    settings = load_settings()
    client = make_readonly_client(settings)
    token_id = args.token_id

    try:
        mid = client.get_midpoint(token_id)
        buy_px = client.get_price(token_id, side="BUY")
        sell_px = client.get_price(token_id, side="SELL")
        book = client.get_order_book(token_id)
    except PolyApiException as exc:
        if getattr(exc, "status_code", None) == 404:
            print(
                "No CLOB orderbook for this token_id (resolved market, wrong id, or "
                "order book disabled). Use a token_id from an active CLOB market — see "
                "https://docs.polymarket.com/developers/gamma-markets-api/get-markets",
                file=sys.stderr,
            )
            raise SystemExit(1) from exc
        raise

    print("token_id:", token_id)
    print("midpoint:", mid)
    print("price BUY:", buy_px, " SELL:", sell_px)
    bids = _top_levels(getattr(book, "bids", None))
    asks = _top_levels(getattr(book, "asks", None))
    print("top bids:", json.dumps(bids, default=str)[:2000])
    print("top asks:", json.dumps(asks, default=str)[:2000])


if __name__ == "__main__":
    main()
