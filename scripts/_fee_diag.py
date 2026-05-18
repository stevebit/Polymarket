"""Diagnose taker fee accounting from a backtest JSON export."""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "reports/backtest_isotonic_dual_anchor.json")
    d = json.loads(path.read_text(encoding="utf-8"))
    fills = d["fills_taker"]
    n = len(fills)
    wins = sum(1 for f in fills if f["won"])
    losses = n - wins

    sum_notional = sum(f["notional_usd"] for f in fills)
    sum_fees = sum(f["fee_usd"] for f in fills)
    sum_pnl = sum(f["realised_pnl_usd"] for f in fills)

    print(f"n fills: {n}  wins: {wins}  losses: {losses}")
    print(f"sum notional: ${sum_notional:,.2f}")
    print(f"sum fees:     ${sum_fees:,.2f}  ({sum_fees / sum_notional:.2%} of notional)")
    print(f"sum realised pnl (excl fees): ${sum_pnl:,.2f}")
    print(f"net (sum pnl - sum fees):     ${sum_pnl - sum_fees:,.2f}")

    fee_per_dollar = [f["fee_usd"] / f["notional_usd"] for f in fills if f["notional_usd"] > 0]
    print(
        f"\nfee/notional ratio:  "
        f"min={min(fee_per_dollar):.3f}  "
        f"med={statistics.median(fee_per_dollar):.3f}  "
        f"mean={statistics.mean(fee_per_dollar):.3f}  "
        f"max={max(fee_per_dollar):.3f}"
    )

    fee_won = [f["fee_usd"] for f in fills if f["won"]]
    fee_lost = [f["fee_usd"] for f in fills if not f["won"]]
    if fee_won:
        print(f"\nmean fee | WIN:  ${statistics.mean(fee_won):,.3f}  (n={len(fee_won)})")
    if fee_lost:
        print(f"mean fee | LOSS: ${statistics.mean(fee_lost):,.3f}  (n={len(fee_lost)})")

    print("\nsample winning fills:")
    for f in [x for x in fills if x["won"]][:6]:
        print(
            f"  {f['station_slug']:<14} price={f['price']:.4f} "
            f"shares={f['shares']:.0f} notional=${f['notional_usd']:.2f} "
            f"fee=${f['fee_usd']:.2f} pnl=${f['realised_pnl_usd']:+.2f}"
        )
    print("\nsample losing fills:")
    for f in [x for x in fills if not x["won"]][:6]:
        print(
            f"  {f['station_slug']:<14} price={f['price']:.4f} "
            f"shares={f['shares']:.0f} notional=${f['notional_usd']:.2f} "
            f"fee=${f['fee_usd']:.2f} pnl=${f['realised_pnl_usd']:+.2f}"
        )

    # Decompose `fee_usd` into true fee + slippage. The backtest currently
    # stores ``fee_total + slippage_total`` under the same column, which makes
    # raw per-station fee accounting look ~20-100x heavier than the V2 CLOB
    # taker schedule actually is. We split them back out here so the per-
    # station NET column reflects a single, configurable assumption.
    # True fee per share = rate * p * (1 - p)   (CLOB v2 symmetric)
    from collections import defaultdict

    taker_rate = d.get("meta", {}).get("fees", {}).get("taker_fee", 0.05)
    slippage_per_share = 0.005  # historical default at the time of the export
    print(
        f"\nAssumptions for split: taker_rate={taker_rate:.4f}  "
        f"default slippage_per_share=${slippage_per_share:.4f}/share"
    )

    by_st = defaultdict(lambda: {"n": 0, "pnl": 0.0, "true_fee": 0.0,
                                  "slip": 0.0, "wins": 0})
    for f in fills:
        s = f["station_slug"]
        price = f["price"]
        shares = f["shares"]
        true_fee_per_share = taker_rate * price * (1.0 - price) if 0 < price < 1 else 0.0
        true_fee = true_fee_per_share * shares
        slip = shares * slippage_per_share
        by_st[s]["n"] += 1
        by_st[s]["pnl"] += f["realised_pnl_usd"]
        by_st[s]["true_fee"] += true_fee
        by_st[s]["slip"] += slip
        by_st[s]["wins"] += 1 if f["won"] else 0

    sum_true_fee = sum(v["true_fee"] for v in by_st.values())
    sum_slip = sum(v["slip"] for v in by_st.values())
    print(
        f"reconstructed: sum true_fee=${sum_true_fee:,.2f}  "
        f"sum slippage=${sum_slip:,.2f}  "
        f"total=${sum_true_fee + sum_slip:,.2f}  "
        f"(JSON reports ${sum_fees:,.2f})"
    )

    print(
        "\nPer-station NET PnL — three views: gross | net (true fee only) | "
        "net (fee + 0.5c slippage)"
    )
    print(
        f"{'station':<14} {'n':>5} {'wins':>5} {'gross':>11} "
        f"{'true_fee':>10} {'slip':>10} {'net_fee_only':>14} {'net_fee_slip':>14}"
    )
    rows = sorted(
        by_st.items(),
        key=lambda kv: -(kv[1]["pnl"] - kv[1]["true_fee"]),
    )
    sum_net_fee_only = 0.0
    sum_net_full = 0.0
    for st, v in rows:
        net_fee_only = v["pnl"] - v["true_fee"]
        net_full = net_fee_only - v["slip"]
        sum_net_fee_only += net_fee_only
        sum_net_full += net_full
        print(
            f"{st:<14} {v['n']:>5} {v['wins']:>5} "
            f"${v['pnl']:>9,.2f} ${v['true_fee']:>8,.2f} ${v['slip']:>8,.2f} "
            f"${net_fee_only:>12,.2f} ${net_full:>12,.2f}"
        )
    print(
        f"{'TOTAL':<14} {n:>5} {wins:>5} "
        f"${sum_pnl:>9,.2f} ${sum_true_fee:>8,.2f} ${sum_slip:>8,.2f} "
        f"${sum_net_fee_only:>12,.2f} ${sum_net_full:>12,.2f}"
    )


if __name__ == "__main__":
    main()
