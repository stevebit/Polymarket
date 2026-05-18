"""Sweep the (min_taker_price, max_taker_price) band on the dual-anchor
backtest and print the resulting fills, gross PnL, true fees, and net PnL.

Runs ``polymarket_weather.cli.backtest`` repeatedly with one configuration
per band. Cached isotonic / maker-fill fits mean each run is ~10 s, so
sweeping across ~6 bands is well under 2 minutes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

BANDS = [
    (None, None),       # baseline: no filter
    (0.02, 0.98),
    (0.05, 0.95),
    (0.08, 0.92),
    (0.10, 0.90),
    (0.15, 0.85),
    (0.20, 0.80),
]
WINDOW = ("2025-04-20", "2026-05-17")
MODEL = "m2_postprocessed_ens"


def run_one(lo: float | None, hi: float | None) -> dict:
    tag = (
        "nofilter"
        if (lo is None and hi is None)
        else f"{lo:.2f}_{hi:.2f}".replace(".", "")
    )
    out = Path(f"reports/backtest_band_{tag}.json")
    cmd = [
        sys.executable, "-u", "-m", "polymarket_weather.cli.backtest",
        "--start", WINDOW[0], "--end", WINDOW[1],
        "--model", MODEL,
        "--strategy", "both",
        "--take-every-n-snapshots", "5",
        "--min-snapshot-utc-hour", "0",
        "--slippage-per-share", "0.001",
        "--export-json", str(out),
        "--print-only",
    ]
    if lo is not None:
        cmd += ["--min-taker-price", f"{lo:.4f}"]
    if hi is not None:
        cmd += ["--max-taker-price", f"{hi:.4f}"]
    # Force UTF-8 on Windows so the post-run markdown render (which uses
    # the U+2212 "minus" glyph) does not crash on cp1252 consoles. The
    # backtest JSON is written before the render step, so a render crash
    # would not necessarily corrupt the JSON, but we want a clean run.
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if not out.exists():
        sys.stderr.write(
            f"[sweep] backtest failed for band ({lo}, {hi}); "
            f"stdout tail:\n{proc.stdout[-1500:]}\n"
            f"stderr tail:\n{proc.stderr[-1500:]}\n"
        )
        raise SystemExit(proc.returncode or 1)
    return json.loads(out.read_text(encoding="utf-8"))


def main() -> None:
    rows = []
    for lo, hi in BANDS:
        d = run_one(lo, hi)
        s = d["summary"]
        fills = d["fills_taker"]
        true_fee = sum(
            0.05 * f["price"] * (1.0 - f["price"]) * f["shares"]
            for f in fills
            if 0 < f["price"] < 1
        )
        gross = s["pnl_taker_usd"] + s["pnl_maker_usd"]
        rows.append(
            {
                "lo": lo, "hi": hi,
                "n_fills": s["n_fills_taker"],
                "n_maker": s["n_fills_maker"],
                "wins": sum(1 for f in fills if (f.get("won") and f.get("side") == "taker_buy") or (not f.get("won") and f.get("side") == "taker_sell")),
                "gross": gross,
                "true_fee": true_fee,
                "net_true_fee": gross - true_fee,
                "log_loss": s.get("realised_log_loss"),
            }
        )

    print(f"{'lo':>5} {'hi':>5} {'n_taker':>8} {'n_mkr':>5} {'wins':>5} "
          f"{'gross':>10} {'true_fee':>9} {'net_true_fee':>13} {'log_loss':>9}")
    for r in rows:
        lo_s = f"{r['lo']:.2f}" if r['lo'] is not None else "  -"
        hi_s = f"{r['hi']:.2f}" if r['hi'] is not None else "  -"
        ll = f"{r['log_loss']:.4f}" if r['log_loss'] is not None else "    -"
        print(
            f"{lo_s:>5} {hi_s:>5} "
            f"{r['n_fills']:>8d} {r['n_maker']:>5d} {r['wins']:>5d} "
            f"${r['gross']:>8,.2f} ${r['true_fee']:>7,.2f} "
            f"${r['net_true_fee']:>11,.2f} {ll:>9}"
        )


if __name__ == "__main__":
    main()
