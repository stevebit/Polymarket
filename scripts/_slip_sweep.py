"""Tiny slippage-sensitivity sweep on top of the cached backtest.

Re-runs ``polymarket_weather.cli.backtest`` with several
``--slippage-per-share`` settings and prints the headline numbers side by
side so we can see how stable the result is to that one parameter.

Output JSONs land in ``reports/backtest_slipsweep_{slip:.4f}.json``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SLIPS = [0.0, 0.001, 0.002, 0.005, 0.010]
WINDOW = ("2025-04-20", "2026-05-17")
MODEL = "m2_postprocessed_ens"


def run_one(slip: float) -> dict:
    out = Path(f"reports/backtest_slipsweep_{slip:.4f}.json")
    cmd = [
        sys.executable, "-u", "-m", "polymarket_weather.cli.backtest",
        "--start", WINDOW[0], "--end", WINDOW[1],
        "--model", MODEL,
        "--strategy", "both",
        "--take-every-n-snapshots", "5",
        "--min-snapshot-utc-hour", "0",
        "--slippage-per-share", f"{slip:.4f}",
        "--export-json", str(out),
        "--print-only",
    ]
    print(f"\n>>> slippage={slip:.4f} <<<")
    res = subprocess.run(cmd, check=True, capture_output=True, text=True)
    last = res.stdout.strip().splitlines()[-1]
    print(last)
    return json.loads(out.read_text(encoding="utf-8"))


def main() -> None:
    results = []
    for slip in SLIPS:
        d = run_one(slip)
        s = d.get("summary", {})
        results.append({"slip": slip, **s})

    print("\n=== summary table ===")
    print(
        f"{'slip':>6} {'fills':>7} {'gross':>11} "
        f"{'fees+slip':>11} {'net':>11} {'log_loss':>9}"
    )
    for r in results:
        print(
            f"{r['slip']:>6.4f} "
            f"{r['n_fills_taker']:>7d} "
            f"${r['pnl_taker_usd']:>9,.2f} "
            f"${r['fees_paid_usd']:>9,.2f} "
            f"${r['net_pnl_usd']:>9,.2f} "
            f"{r['realised_log_loss']:>9.4f}"
        )


if __name__ == "__main__":
    main()
