"""Audit the (side, won, pnl) consistency on backtest fills.

We've seen cases like `won=True, price=0.999, pnl=-$5` which is inconsistent
with TAKER_BUY YES math. This script enumerates the four (action x outcome)
quadrants and prints sample fills + checks that ``pnl ~ (1 if won else 0) - price``
holds for taker fills.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "reports/backtest_isotonic_slip1tick.json")
    d = json.loads(path.read_text(encoding="utf-8"))
    fills = d["fills_taker"]
    print(f"path: {path}")
    print(f"n fills: {len(fills)}")

    # 1) Breakdown by (side, won)
    quads = Counter()
    for f in fills:
        quads[(f["side"], f["won"])] += 1
    print("\n(side, won) breakdown:")
    for k, v in sorted(quads.items()):
        print(f"  {k}: {v}")

    # 2) Sample one fill per quadrant
    print("\nSample fill per quadrant (raw JSON):")
    seen = set()
    for f in fills:
        k = (f["side"], f["won"])
        if k in seen:
            continue
        seen.add(k)
        print(f"\n  -- {k} --")
        for kk, vv in f.items():
            print(f"     {kk}: {vv!r}")

    # 3) For TAKER_BUY, check pnl matches (1 if won else 0 - price) * shares.
    #    Tolerance 1c per fill.
    print("\nTAKER_BUY pnl consistency check (vs payoff = won ? 1 : 0):")
    n_ok = 0
    n_bad = 0
    bad_examples = []
    for f in fills:
        if f["side"] != "taker_buy":
            continue
        if not f["settled"]:
            continue
        expected = ((1.0 if f["won"] else 0.0) - f["price"]) * f["shares"]
        actual = f["realised_pnl_usd"]
        if abs(expected - actual) < 0.011:
            n_ok += 1
        else:
            n_bad += 1
            if len(bad_examples) < 6:
                bad_examples.append(
                    {
                        "station": f["station_slug"],
                        "bucket": f["bucket_label"],
                        "realised": f["realised_label"],
                        "won": f["won"],
                        "price": f["price"],
                        "shares": f["shares"],
                        "expected": expected,
                        "actual": actual,
                        "p_model_at_post": f["p_model_at_post"],
                        "expected_pnl_per_share_at_post": f["expected_pnl_per_share_at_post"],
                    }
                )
    print(f"  matches:  {n_ok}")
    print(f"  mismatch: {n_bad}")
    for ex in bad_examples:
        print("    ", ex)


if __name__ == "__main__":
    main()
