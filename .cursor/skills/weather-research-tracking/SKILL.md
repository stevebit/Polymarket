---
name: weather-research-tracking
description: >-
  Polymarket weather research — replay/backtest alignment, known failures, and
  what to rerun when improving the system. Use when working on predict_history,
  backtest, calibration, or Open-Meteo / RAP ingest.
---

# Weather research — status & runbook

Update this file as behaviors change (add dated rows; do not delete history without a reason).

## Book time alignment (model `run_time` vs `pm_market_snapshots`)

| Topic | Status | Notes |
|-------|--------|--------|
| **Intraday CLOB + same-day probs** | **Working** | Use `python -m polymarket_weather.cli.predict_history ... --book-alignment utc-midnight` so `run_time` is **00:00 UTC** on the anchor day. Then morning snapshots can use `bucket_probs` under `run_time <= snapshot_at`. |
| **Noon anchor (legacy)** | **Working** | `--book-alignment utc-noon` or `--cutoff-hour 12` with `--book-alignment from-cutoff-hour`. Pair with `python -m polymarket_weather.cli.backtest ... --min-snapshot-utc-hour 12` so books are only **at or after** model release, **or** accept that early-day snapshots skip. |
| **Mixing midnight + noon rows** | **Caveat** | Both can exist in `predictions` / `bucket_probs` for the same `(model, station, target_date)`. Backtest/calibration `DISTINCT ON` paths must pick the semantics you want; prefer **one alignment per experiment**. |
| **`n_event_snapshots_with_probs` = 0** | **Usually misaligned clocks** | Check snapshot UTC hour vs `predict_history` cutoff. |

## CLI flags (reference)

- `predict_history`: `--book-alignment {from-cutoff-hour,utc-midnight,utc-noon}`; `--cutoff-hour` applies only with `from-cutoff-hour`.
- `backtest`: `--take-every-n-snapshots 1` for full book cadence; `--min-snapshot-utc-hour H` to filter snapshots (UTC hour ≥ H).
- `rebuild_research_data.py`: `--predict-book-alignment …` forwards to `predict_history`.

## Data / ingest

| Topic | Status | Notes |
|-------|--------|--------|
| **RAP nowcast `rap_conus` live API** | **Flaky / 400 observed** | Open-Meteo returned HTTP 400 for `models=rap_conus` during some `predict_history` replays; logged as `RAP nowcast failed`. Replay still completes; M2 may omit RAP component that day. Revisit model id / endpoint when improving nowcast. |
| **Azure Postgres drops** | **Mitigated** | Pool init fixed in `db.py`; `predict_history` retries transient `OperationalError` / `PoolTimeout`. |
| **WU / NOOA resolution** | **Working** | `ingest_resolution_observations`, `observations` for settlement. |

## Backtest sanity checks

1. `snapshot_stats.n_event_snapshots_with_probs` should be **> 0** when trades are expected.
2. If fills are zero, confirm Gamma/CLOB snapshots exist for the window and `bucket_probs` exist for **M2** (or chosen `--model`) with correct `run_time` ordering.

## Verification log (append rows)

| Date (UTC) | Change checked | Result |
|------------|----------------|--------|
| 2026-05-15 | CLI: `--book-alignment`, `--min-snapshot-utc-hour`, `rebuild_research_data --predict-book-alignment` | Landed in repo; `pytest` 51 passed. |
| 2026-05-15 | `take_every_n=1` backtest May 1–14 | Done: `reports/backtest_round3_full_cadence.json`, `backtest_20260515T063636Z.md`; exit 0; ~58m wall time; 453 taker / 50 maker fills (see report for PnL). |
| 2026-05-15 | `utc-midnight` replay May 2026 + backtest `take_every_n=5` | `n_event_snapshots_with_probs` ≫ 0; PnL non-zero (strategic, not diagnostic). |
| | `take_every_n_snapshots` 1 | Run after full replay for production-like density. |
