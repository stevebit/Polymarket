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
| 2026-05-18 | `calibrate --lookback-days 365 --include-market` on M0/M1/M2/market | First non-empty `calibration_runs`. n=845 across all four; log-loss M0=1.540, M1=1.400, M2=1.431, market:mid=1.548. M1/M2 beat market on log-loss; market beats us on Brier. Reliability bins show under-confidence in `[0.3, 0.6]` (predicted ≈ 0.45 vs observed ≈ 0.55) — isotonic recalibration (`006_isotonic_calibration`) is the next sharpener. |
| 2026-05-18 | `fit_postprocess --lookback-days 365 --leads 0..7` | `Fits=77 skipped_too_few=284 skipped_no_data=1399`. **Lead-1+ is structurally empty** in `forecasts` (historical backfill stored only `run_time == target_date`; lead-1+ rows trickle in only from the rolling 7–14 d of live `ingest_forecasts`, all below the `MIN_TRAIN_PAIRS=30` threshold). To fit lead-1+ EMOS, extend `scripts/backfill_open_meteo_history.py` to walk Open-Meteo's `past_days` cursor across multiple issue dates. |
| 2026-05-18 | `ingest_ensembles --past-days 7 --forecast-days 7` | First non-empty `forecast_members`: 8 591 rows for `openmeteo_ens:gfs025` (3 751) + `openmeteo_ens:icon_seamless` (4 840). `ecmwf_ifs04` and `gem_seamless` returned deterministic-only series (no `temperature_2m_memberNN` keys) — pre-existing Open-Meteo behaviour, not introduced by us. `persist_members` switched to `executemany` (2 min vs 30 min). |
| 2026-05-18 | Intraday dual-anchor backtest `2025-04-20..2026-05-17`, M2, `--strategy both --take-every-n-snapshots 5` | `reports/backtest_intraday_dual_anchor.json` + `backtest_20260518T035036Z.md`. 845 resolved events, 1 542 taker + 5 maker fills, taker PnL **-$1 397**, fees **$7 649**, **net -$9 046**, max DD -$3 776. Realised log-loss **1.4288** ≈ `calibrate` `m2_postprocessed_ens=1.4306` — alignment confirmed. Per-station: LA +$4 601, all others $-373..-$780 (fee bleed dominates gross-positive cells). Maker fill rate dropped to 0.4 % with `take_every_n=5`. |
| | `take_every_n_snapshots` 1 | Run after full replay for production-like density. Estimated ~5 h wall time for `2025-04-20..2026-05-17` if you want maker fills back up to 8–17 %. |
