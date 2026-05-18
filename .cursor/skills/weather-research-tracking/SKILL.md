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
| 2026-05-18 | Replay calibration sweep: stale LA-68-69°F per-station artefact, slippage sensitivity, price-tail concentration, new replay-time `--min-taker-price` / `--max-taker-price` filters, price-band sweep over 2025-04-20..2026-05-17 with M2 ensemble at `--take-every-n-snapshots 5`. | Earlier "LA per-station +$4,602" headline was traced to a single replay fill (`los-angeles 68-69°F`, taker_buy at price 0.001, 4999 shares, won=True, +$4994) in `reports/backtest_intraday_dual_anchor.json` and `reports/backtest_isotonic_dual_anchor.json`; after `recalibrate_bucket_probs` rewrote `bucket_probs` that synthetic fill no longer materialises — see `reports/backtest_band_nofilter.json`, where the only remaining LA 68-69°F rows are -$5 fills. So this was a stale-data artefact, not a station-level signal. Realised replay PnL is also flat across `--slippage-per-share ∈ {0, 0.001, 0.002, 0.005, 0.010}` (about $3 PnL spread across `reports/backtest_slipsweep_0.{0000,0010,0020,0050,0100}.json`), meaning the slippage knob currently moves only the on-disk `fee_usd` book-keeping, not which edges actually fire. Price-tail diagnostic (`scripts/_price_bins.py` on `backtest_band_nofilter.json`): 1467 taker fills execute at price <1c with cumulative realised PnL -$5,434.82, 47 fills execute at price ≥99c with -$195.20, and only 22 fills sit in the 1c..99c interior (+$13.74) — i.e. ~98.6% of fills and essentially all of the negative replay PnL live in the tails. Added `--min-taker-price` / `--max-taker-price` CLI flags on `polymarket_weather/cli/backtest.py` that thread into `replay_backtest` in `polymarket_weather/backtest.py` (validated and applied at the edge-evaluation loop ~line 630, surfaced into `snapshot_stats` and notes). Price-band sweep result (`scripts/_price_band_sweep.py` → `reports/price_band_sweep.log`): lo,hi,n_taker,gross,true_fee,net_true_fee,log_loss = (-,-,1536,$-5,616.28,$319.11,$-5,935.39,1.1453) / (0.02,0.98,24,$68.71,$2.97,$65.73,1.1453) / (0.05,0.95,19,$72.23,$2.64,$69.59,1.1453) / (0.08,0.92,17,$81.82,$2.19,$79.63,1.1453) / (0.10,0.90,15,$88.21,$1.90,$86.31,1.1453) / (0.15,0.85,13,$67.58,$1.61,$65.97,1.1453) / (0.20,0.80,13,$8.14,$1.32,$6.82,1.1453). Best net-of-true-V2-fee band is [0.10, 0.90] with 15 fills, gross $88.21, true_fee $1.90, net_true_fee $86.31; per-station decomposition from `scripts/_fee_diag.py reports/backtest_band_010_090.json` shows dallas/houston/seattle/miami/san-francisco net positive, austin/nyc/atlanta/los-angeles net negative under the inner band. Realised log-loss is unchanged across all bands at 1.1453 because the price filter only suppresses taker fills, not which probabilities the model writes. Sample size remains small (sub-50 fills/year per band at current per-bucket / per-event caps), so the positive inner-band bands are research signals to investigate further, not production-ready conclusions. Pre-commit subset (`tests/test_no_lookahead.py`, `tests/test_fee_formula.py`, `tests/test_cli_date_parse.py`, `tests/test_phase2_misc.py`) passes 27/27. |
| | `take_every_n_snapshots` 1 | Run after full replay for production-like density. |
