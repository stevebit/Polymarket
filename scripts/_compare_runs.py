"""Compare two backtest JSON exports fill-by-fill.

Helpful when slippage / model / window changes and we want to see which
fills are present in one run but not the other, and how the (side, won)
quadrants shift.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def fkey(f: dict) -> tuple:
    return (
        f["event_slug"],
        f["station_slug"],
        f["bucket_label"],
        f["target_date"],
        f["side"],
        round(f["price"], 5),
        round(f["shares"], 3),
    )


def main() -> None:
    a_path = Path(sys.argv[1])
    b_path = Path(sys.argv[2])
    a = json.loads(a_path.read_text(encoding="utf-8"))
    b = json.loads(b_path.read_text(encoding="utf-8"))

    a_fills = a["fills_taker"]
    b_fills = b["fills_taker"]
    a_keys = {fkey(f) for f in a_fills}
    b_keys = {fkey(f) for f in b_fills}

    a_only = a_keys - b_keys
    b_only = b_keys - a_keys
    both = a_keys & b_keys

    print(f"A: {a_path.name}  n={len(a_fills)}")
    print(f"B: {b_path.name}  n={len(b_fills)}")
    print(f"Common keys:  {len(both)}")
    print(f"A only:       {len(a_only)}")
    print(f"B only:       {len(b_only)}")

    def quadrants(rows: list[dict], keyset: set | None = None) -> Counter:
        c = Counter()
        for f in rows:
            if keyset is not None and fkey(f) not in keyset:
                continue
            c[(f["side"], f["won"])] += 1
        return c

    print("\nA full quadrants:", dict(quadrants(a_fills)))
    print("B full quadrants:", dict(quadrants(b_fills)))
    print("A only quadrants:", dict(quadrants(a_fills, a_only)))
    print("B only quadrants:", dict(quadrants(b_fills, b_only)))

    # Aggregate pnl by source
    def total_pnl(rows: list[dict], keyset: set | None = None) -> float:
        return sum(
            f["realised_pnl_usd"]
            for f in rows
            if keyset is None or fkey(f) in keyset
        )

    print("\nSum realised_pnl_usd:")
    print(f"  A full:  ${total_pnl(a_fills):+,.2f}")
    print(f"  B full:  ${total_pnl(b_fills):+,.2f}")
    print(f"  A only:  ${total_pnl(a_fills, a_only):+,.2f}")
    print(f"  B only:  ${total_pnl(b_fills, b_only):+,.2f}")
    print(f"  A common:${total_pnl(a_fills, both):+,.2f}")
    print(f"  B common:${total_pnl(b_fills, both):+,.2f}")

    print("\n--- A-only fills detail (in run A but not B) ---")
    a_only_fills = [f for f in a_fills if fkey(f) in a_only]
    for f in sorted(a_only_fills, key=lambda x: -x["realised_pnl_usd"]):
        print(
            f"  {f['station_slug']:<14} {f['side']:<11} "
            f"price={f['price']:.4f} sh={f['shares']:>5.0f} "
            f"p_model={f['p_model_at_post']:.3f} won={f['won']!s:<5} "
            f"pnl=${f['realised_pnl_usd']:+,.2f}  "
            f"bucket={f['bucket_label']!r}"
        )

    print("\n--- B-only fills detail (in run B but not A) ---")
    b_only_fills = [f for f in b_fills if fkey(f) in b_only]
    for f in sorted(b_only_fills, key=lambda x: -x["realised_pnl_usd"]):
        print(
            f"  {f['station_slug']:<14} {f['side']:<11} "
            f"price={f['price']:.4f} sh={f['shares']:>5.0f} "
            f"p_model={f['p_model_at_post']:.3f} won={f['won']!s:<5} "
            f"pnl=${f['realised_pnl_usd']:+,.2f}  "
            f"bucket={f['bucket_label']!r}"
        )


if __name__ == "__main__":
    main()
