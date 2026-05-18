"""Run a grid of backtests over (station-whitelist, min-edge-cents) and
print a single comparison table. Intended for the post-isotonic tuning
sweep documented in ``docs/PROGRESS_2026_05_18.md``.

Each row reuses the same date window and ``--take-every-n-snapshots 5``
intraday dual-anchor settings; only the station and min-edge gate vary.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from polymarket_weather import config
from polymarket_weather.backtest import (
    backtest_result_to_dict,
    replay_backtest,
)
from polymarket_weather.models.m2_postprocessed_ensemble import MODEL_M2
from polymarket_weather.strategy.edge import FeeSchedule
from polymarket_weather.strategy.sizing import CapsConfig


START = dt.date(2025, 4, 20)
END = dt.date(2026, 5, 17)
MODEL = MODEL_M2


def caps_with_min_edge(cents: float) -> CapsConfig:
    return CapsConfig(
        bankroll_usd=500.0,
        per_bucket_usd=5.0,
        per_event_usd=20.0,
        per_day_usd=100.0,
        per_portfolio_usd=500.0,
        kelly_fraction=0.25,
        min_edge_per_dollar=cents / 100.0,
    )


def run_grid_cell(
    stations: list[str],
    min_edge_cents: float,
    *,
    label: str,
) -> dict:
    print(f"\n=== {label} | stations={stations} min_edge={min_edge_cents}c ===")
    caps = caps_with_min_edge(min_edge_cents)
    fees = FeeSchedule()
    result = replay_backtest(
        model_id=MODEL,
        station_slugs=stations,
        start=START,
        end=END,
        caps=caps,
        fees=fees,
        strategy="both",
        target_spread=0.04,
        take_every_n_snapshots=5,
        slippage_per_share=0.005,
        min_snapshot_utc_hour=0,
    )
    payload = backtest_result_to_dict(
        result, model_id=MODEL, start=START, end=END,
        strategy="both", caps=caps, fees=fees,
    )
    s = payload["summary"]
    print(
        f"  taker_pnl=${s['pnl_taker_usd']:+.2f} "
        f"maker_pnl=${s['pnl_maker_usd']:+.2f} "
        f"net=${s['net_pnl_usd']:+.2f} "
        f"fees=${s['fees_paid_usd']:.2f} "
        f"fills={s['n_fills_taker']}t/{s['n_fills_maker']}m "
        f"events={s['n_events_resolved']} "
        f"log_loss={s['realised_log_loss']:.4f}"
    )
    return {"label": label, "stations": stations, "min_edge_cents": min_edge_cents, **s}


def main() -> None:
    out_dir = Path("reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    profitable = ["los-angeles", "san-francisco"]
    expanded = profitable + ["houston", "austin", "denver"]
    all_us = config.station_slugs()

    cells = []
    cells.append(run_grid_cell(all_us, 2.0, label="all_stations_min2c (baseline)"))
    cells.append(run_grid_cell(all_us, 15.0, label="all_stations_min15c"))
    cells.append(run_grid_cell(all_us, 30.0, label="all_stations_min30c"))
    cells.append(run_grid_cell(all_us, 50.0, label="all_stations_min50c"))
    cells.append(run_grid_cell(profitable, 2.0, label="LA+SF_min2c"))
    cells.append(run_grid_cell(profitable, 15.0, label="LA+SF_min15c"))
    cells.append(run_grid_cell(profitable, 30.0, label="LA+SF_min30c"))
    cells.append(run_grid_cell(expanded, 15.0, label="LA+SF+H+A+D_min15c"))
    cells.append(run_grid_cell(expanded, 30.0, label="LA+SF+H+A+D_min30c"))
    cells.append(run_grid_cell(["los-angeles"], 2.0, label="LA_only_min2c"))
    cells.append(run_grid_cell(["los-angeles"], 15.0, label="LA_only_min15c"))

    print("\n=== SUMMARY ===")
    print(
        f"{'label':<35} {'net':>10} {'taker':>10} {'fees':>10} "
        f"{'fills':>8} {'log_loss':>9}"
    )
    for c in sorted(cells, key=lambda x: -x["net_pnl_usd"]):
        print(
            f"{c['label']:<35} "
            f"${c['net_pnl_usd']:>+9.0f} "
            f"${c['pnl_taker_usd']:>+9.0f} "
            f"${c['fees_paid_usd']:>9.0f} "
            f"{c['n_fills_taker']:>8} "
            f"{c['realised_log_loss']:>9.4f}"
        )

    out_path = out_dir / "backtest_grid_isotonic.json"
    out_path.write_text(json.dumps(cells, indent=2, default=str))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
