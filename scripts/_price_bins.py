"""Bin taker fills by price + side and print gross PnL by bin.

Reveals whether the strategy's gross loss is concentrated at extreme
prices (e.g. taker_buy at 0.005 = "long lottery tickets") vs the more
moderate price regime where the V2 fee schedule is highest but per-fill
notional is more reasonable.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path


PRICE_BINS = [
    (0.0, 0.01, "[0,1c)"),
    (0.01, 0.05, "[1c,5c)"),
    (0.05, 0.15, "[5c,15c)"),
    (0.15, 0.40, "[15c,40c)"),
    (0.40, 0.60, "[40c,60c)"),
    (0.60, 0.85, "[60c,85c)"),
    (0.85, 0.95, "[85c,95c)"),
    (0.95, 0.99, "[95c,99c)"),
    (0.99, 1.01, "[99c,100c]"),
]


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "reports/backtest_slipsweep_0.0010.json")
    d = json.loads(path.read_text(encoding="utf-8"))
    fills = d["fills_taker"]
    print(f"path: {path}  n_fills: {len(fills)}")

    by_bin = defaultdict(lambda: {"n": 0, "notional": 0.0, "pnl": 0.0,
                                   "wins": 0, "loss_dollars": 0.0})
    for f in fills:
        p = f["price"]
        bin_label = next(lbl for lo, hi, lbl in PRICE_BINS if lo <= p < hi)
        key = (f["side"], bin_label)
        by_bin[key]["n"] += 1
        by_bin[key]["notional"] += f["notional_usd"]
        by_bin[key]["pnl"] += f["realised_pnl_usd"]
        by_bin[key]["wins"] += 1 if f["won"] else 0
        if f["realised_pnl_usd"] < 0:
            by_bin[key]["loss_dollars"] += -f["realised_pnl_usd"]

    print(
        f"\n{'side':<12} {'price':<12} {'n':>5} {'wins':>5} "
        f"{'notional':>12} {'gross_pnl':>11} {'pnl/fill':>9}"
    )
    rows = sorted(by_bin.items(), key=lambda kv: (kv[0][0], kv[0][1]))
    for (side, lbl), v in rows:
        avg = v["pnl"] / v["n"] if v["n"] else 0
        print(
            f"{side:<12} {lbl:<12} {v['n']:>5} {v['wins']:>5} "
            f"${v['notional']:>9,.2f} ${v['pnl']:>9,.2f} ${avg:>7,.2f}"
        )

    # Restricted P&L: drop both tails (0,1c) and (99c,100c]
    print("\n--- P&L if we drop ALL fills with price < 1c or >= 99c ---")
    keep = [f for f in fills if 0.01 <= f["price"] < 0.99]
    drop = [f for f in fills if not (0.01 <= f["price"] < 0.99)]
    print(f"  kept: {len(keep)} fills, "
          f"gross_pnl=${sum(f['realised_pnl_usd'] for f in keep):+,.2f}")
    print(f"  dropped: {len(drop)} fills, "
          f"gross_pnl=${sum(f['realised_pnl_usd'] for f in drop):+,.2f}")


if __name__ == "__main__":
    main()
