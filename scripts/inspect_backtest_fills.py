"""Quick inspector for a backtest JSON export.

Prints worst sells and worst buys side-by-side so a sizing bug between
``edge.price`` and the actual loss-if-wrong exposure becomes obvious.

Usage:

    .\\.venv\\Scripts\\python.exe .\\scripts\\inspect_backtest_fills.py reports/m1_run.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: inspect_backtest_fills.py <path-to-backtest.json>")
        sys.exit(2)
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    fills = data.get("fills_taker", [])
    settled = [f for f in fills if f.get("settled")]
    print(f"Total taker fills: {len(fills)}, settled: {len(settled)}")
    print()
    print("--- TAKER_SELL fills sorted by realised_pnl_usd (worst first) ---")
    sells = sorted(
        [f for f in fills if f["side"] == "taker_sell"],
        key=lambda x: x["realised_pnl_usd"],
    )
    for f in sells[:10]:
        notional_yes = f["price"] * f["shares"]
        actual_exposure_no = (1 - f["price"]) * f["shares"]
        print(
            f"  pnl={f['realised_pnl_usd']:+9.2f}  "
            f"yes_price={f['price']:.3f}  shares={f['shares']:6d}  "
            f"shares*price=${notional_yes:7.2f}  "
            f"shares*(1-price)=${actual_exposure_no:8.2f}  "
            f"won={f['won']}  "
            f"bucket={f['bucket_label'][:25]}"
        )
    print()
    print("--- TAKER_BUY fills sorted by realised_pnl_usd (worst first) ---")
    buys = sorted(
        [f for f in fills if f["side"] == "taker_buy"],
        key=lambda x: x["realised_pnl_usd"],
    )
    for f in buys[:5]:
        notional = f["price"] * f["shares"]
        print(
            f"  pnl={f['realised_pnl_usd']:+9.2f}  "
            f"price={f['price']:.3f}  shares={f['shares']:6d}  "
            f"shares*price=${notional:7.2f}  won={f['won']}  "
            f"bucket={f['bucket_label'][:25]}"
        )
    print()
    total_yes_notional = sum(
        f["price"] * f["shares"] for f in fills
    )
    total_loss_if_wrong = sum(
        f["price"] * f["shares"] if f["side"].endswith("_buy")
        else (1 - f["price"]) * f["shares"]
        for f in fills
    )
    print(
        f"Sum of shares*price across taker fills: ${total_yes_notional:.2f}\n"
        f"Sum of true loss-if-wrong (buy: price; sell: 1-price): "
        f"${total_loss_if_wrong:.2f}"
    )


if __name__ == "__main__":
    main()
