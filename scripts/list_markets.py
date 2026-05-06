"""
List markets from the Polymarket CLOB API (read-only, no .env required).

The HTTP API is paginated; this script follows next_cursor until the end or --max-pages.
"""

from __future__ import annotations

import argparse
import json
import sys

from py_clob_client_v2.constants import END_CURSOR, INITIAL_CURSOR

from polymarket_manual.clients import make_readonly_client
from polymarket_manual.config import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="List CLOB markets with pagination.")
    parser.add_argument(
        "--detailed",
        action="store_true",
        help="Use get_markets() instead of get_simplified_markets() (larger payload).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=200,
        metavar="N",
        help="Safety cap on pagination (default 200).",
    )
    parser.add_argument(
        "--format",
        choices=("summary", "jsonl"),
        default="summary",
        help="summary: one compact line per market; jsonl: one JSON object per line.",
    )
    parser.add_argument(
        "--only-active",
        action="store_true",
        help="Skip markets where accepting_orders is false.",
    )
    parser.add_argument(
        "--only-orderbook",
        action="store_true",
        help="When using --detailed, skip markets where enable_order_book is false.",
    )
    args = parser.parse_args()

    settings = load_settings()
    client = make_readonly_client(settings)

    cursor: str | None = INITIAL_CURSOR
    total = 0
    pages = 0

    while cursor and cursor != END_CURSOR and pages < args.max_pages:
        pages += 1
        if args.detailed:
            batch = client.get_markets(next_cursor=cursor)
        else:
            batch = client.get_simplified_markets(next_cursor=cursor)

        rows = batch.get("data") or []
        for row in rows:
            if args.only_active and not row.get("accepting_orders"):
                continue
            if args.only_orderbook and args.detailed and not row.get("enable_order_book"):
                continue

            total += 1
            if args.format == "jsonl":
                print(json.dumps(row, default=str))
            else:
                q = (row.get("question") or row.get("market_slug") or "")[:72]
                cid = row.get("condition_id", "")
                acc = row.get("accepting_orders")
                ob = row.get("enable_order_book") if args.detailed else "?"
                print(f"{cid}\taccepting={acc}\torderbook={ob}\t{q}")

        cursor = batch.get("next_cursor")
        if not cursor:
            break

    print(f"# pages={pages} markets_printed={total}", file=sys.stderr)


if __name__ == "__main__":
    main()
