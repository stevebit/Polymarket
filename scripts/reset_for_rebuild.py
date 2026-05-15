"""Clear derived tables before the Phase 4 honest-history rebuild.

The user signed off on a destructive reset of derived data in the
implementation plan: "do whatever you have to do." This script
*truncates* tables that are pure derivatives of forecast / observation /
EMOS state and would be invalidated by the Phase 1 fixes:

* ``bucket_probs``     — bucket-level probabilities (Phase 1b/c/d fixes)
* ``predictions``      — model mean/std rows (Phase 1d run_time fix)
* ``calibration_runs`` — generated from ``bucket_probs`` only
* ``daily_pnl``        — aggregate that we re-derive from fills
* ``paper_trades``     where ``notes LIKE '%BUGGY_SIZING_PRE_FIX%'``
  (already quarantined by ``scripts/quarantine_buggy_paper.py``).

It **preserves** every authoritative dataset:

* ``forecasts``, ``observations``, ``hourly_observations``
* ``pm_events``, ``pm_buckets``, ``pm_market_snapshots``
* ``stations``, ``climatology``, ``postprocess_coefs``

Default is dry-run. Pass ``--apply`` to commit the changes. Pass
``--full-paper`` to also wipe **all** ``paper_trades`` (not just the
buggy ones) — only do this once the Phase 4 rebuild produces new
recommendations.

Usage::

    # Preview:
    .\\.venv\\Scripts\\python.exe .\\scripts\\reset_for_rebuild.py
    # Commit:
    .\\.venv\\Scripts\\python.exe .\\scripts\\reset_for_rebuild.py --apply
    # Also wipe paper_trades:
    .\\.venv\\Scripts\\python.exe .\\scripts\\reset_for_rebuild.py --apply --full-paper
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from polymarket_weather.db import with_conn  # noqa: E402

BUGGY_TAG = "%BUGGY_SIZING_PRE_FIX%"


COUNT_SQL: dict[str, str] = {
    "bucket_probs": "SELECT count(*) FROM bucket_probs",
    "predictions": "SELECT count(*) FROM predictions",
    "calibration_runs": "SELECT count(*) FROM calibration_runs",
    "daily_pnl": "SELECT count(*) FROM daily_pnl",
    "paper_trades_buggy": (
        "SELECT count(*) FROM paper_trades WHERE notes LIKE %s"
    ),
    "paper_trades_all": "SELECT count(*) FROM paper_trades",
}


def _count(cur, key: str, params: tuple = ()) -> int:
    cur.execute(COUNT_SQL[key], params)
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="Commit (default dry-run).")
    p.add_argument(
        "--full-paper",
        action="store_true",
        help="Also wipe all ``paper_trades`` (default keeps clean rows).",
    )
    args = p.parse_args()

    with with_conn() as conn, conn.cursor() as cur:
        before = {
            "bucket_probs": _count(cur, "bucket_probs"),
            "predictions": _count(cur, "predictions"),
            "calibration_runs": _count(cur, "calibration_runs"),
            "daily_pnl": _count(cur, "daily_pnl"),
            "paper_trades_buggy": _count(cur, "paper_trades_buggy", (BUGGY_TAG,)),
            "paper_trades_all": _count(cur, "paper_trades_all"),
        }
        print("Row counts before reset:")
        for k, v in before.items():
            print(f"  {k:24s} {v}")

        if not args.apply:
            print(
                "\nDry-run. Re-run with --apply to truncate the derived tables."
            )
            return

        cur.execute("TRUNCATE bucket_probs")
        cur.execute("TRUNCATE predictions")
        cur.execute("TRUNCATE calibration_runs")
        cur.execute("TRUNCATE daily_pnl")
        if args.full_paper:
            cur.execute("TRUNCATE paper_trades")
        else:
            cur.execute("DELETE FROM paper_trades WHERE notes LIKE %s", (BUGGY_TAG,))
        conn.commit()

        after = {
            "bucket_probs": _count(cur, "bucket_probs"),
            "predictions": _count(cur, "predictions"),
            "calibration_runs": _count(cur, "calibration_runs"),
            "daily_pnl": _count(cur, "daily_pnl"),
            "paper_trades_all": _count(cur, "paper_trades_all"),
        }
        print("\nRow counts after reset:")
        for k, v in after.items():
            print(f"  {k:24s} {v}")
        print("\nNext steps:")
        print("  1) scripts/backfill_open_meteo_history.py")
        print("  2) python -m polymarket_weather.cli.fit_postprocess --lookback-days 365")
        print("  3) python -m polymarket_weather.cli.predict_history --start ...")


if __name__ == "__main__":
    main()
