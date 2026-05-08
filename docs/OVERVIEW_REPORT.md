# Polymarket Weather — Project Overview Report

This document is a **narrative summary** of what was built in this repository, **why** each piece exists, and how the pieces fit together. It complements [Agents.md](../Agents.md) (operational rules for agents) and [PHASE6_GO_LIVE_CHECKLIST.md](PHASE6_GO_LIVE_CHECKLIST.md) (pre-flight before live trading).

---

## 1. What this repo is for

You have **two** related products in one codebase:

1. **`polymarket_manual/`** — small CLI helpers for **manual** Polymarket CLOB trading (`scripts/*.py`). Orders require explicit `--confirm YES`. This path is unchanged in spirit: no silent automation here.

2. **`polymarket_weather/`** — a **daily high temperature** research and (optionally) trading pipeline for Polymarket’s **11 US city** markets. It ingests forecasts and observations into **Azure Postgres**, fits and scores probabilistic models, compares them to **market-implied** probabilities, proposes fee-aware sizes, and can run in **paper mode** or **live mode** behind strict environment gates.

The overarching goal (from the profitable-weather plan) is to move from calibration-only research toward a **tiny-bankroll, capped, kill-switchable** system that either survives **~5% taker fees** or earns **maker liquidity rewards**, with evidence from backtests and paper trading before risking USDC.

---

## 2. Why Azure Postgres

Polymarket weather work is **stateful**: many stations, many days, many forecast sources, snapshots of order books, model runs, and (later) orders and fills. A database gives:

- **Idempotent upserts** (re-run ingest or migrate without duplicating rows).
- **Time-series queries** (latest forecast before event close, historical calibration).
- **A single place** for the orchestrator, reports, and dashboards to read from.

`WEATHER_POSTGRES_URL` in `.env` points at your Azure Database for PostgreSQL flexible server; migrations live under `migrations/`.

**Note:** `.gitignore` uses `/data/` (repo root only) so ad-hoc download folders stay untracked while **`polymarket_weather/data/`** (Python ingest modules) remains in git. Orchestrator logs go under `logs/`, which is ignored.

---

## 3. Schema evolution (migrations)

| Migration | Purpose |
|-----------|---------|
| `001_init.sql` | Core weather + Polymarket discovery: stations, events, buckets, snapshots, forecasts, observations, climatology, predictions, bucket_probs, calibration_runs. |
| `002_ensembles_live.sql` | **Ensemble members** (`forecast_members`) for per-member max temperature traces; **EMOS coefficients** (`postprocess_coefs`) keyed by model, station, source, lead day. |
| `003_paper_trades.sql` | **Paper trades** — simulated recommendations with optional settlement and PnL. |
| `004_orders_fills.sql` | **Live automation**: `orders`, `fills`, `daily_pnl` aggregates. |

Run `python -m polymarket_weather.cli.migrate` on any new machine before other weather CLIs.

---

## 4. Data layer — what we pull and why

| Area | What | Why |
|------|------|-----|
| **Resolution truth** | Weather Underground / weather.com style historical daily max at the **airport** Polymarket uses (`ingest_resolution_observations`) | Aligns training and settlement with **how markets resolve** (whole °F). Parity vs Polymarket outcomes was validated in parity reports. |
| **Historical obs** | NOAA GHCN-Daily (`ingest_observations`) | Long backfill for climatology and model skill vs a stable public record. |
| **Forecasts** | Open-Meteo (multi-model, bestmatch), NWS gridpoint + daily, NBM v5 station bulletins (`ingest_forecasts`) | Rich, **multi-source** view of uncertainty and bias; feeds EMOS and M2. |
| **Ensembles** | Open-Meteo ensemble APIs into `forecast_members` (`ingest_ensembles`) | Real **spread** for post-processing (EMOS uses spread in variance). |
| **Live ASOS** | NWS observations + Iowa Mesonet fallback (`ingest_live_observations`) | **Same-day** “max so far” feature for short-lead nowcasting (not resolution). |
| **Markets** | Gamma + read-only CLOB (`discover`) | Event slugs, token IDs, and **order book snapshots** for calibration vs market and for strategy/backtest. |

Together, this answers: “What did models and the market say, when, and what actually happened?”

---

## 5. Models and scoring — what and why

| Model | Idea | Why it exists |
|-------|------|----------------|
| **M0** | Best single forecast source → Gaussian over daily max | Simple, interpretable baseline. |
| **M1** | Ensemble across sources → Gaussian | Less fragile than one source; still can be **miscalibrated in the tails**. |
| **EMOS** (`postprocess.py`, `fit_postprocess`) | Per (station, source, lead_day): affine mean + log-variance tied to **ensemble spread** | Classical way to **calibrate** ensemble forecasts to observations; coefficients persisted in `postprocess_coefs`. |
| **M2** (`m2_postprocessed_ensemble.py`) | Mixture of EMOS-corrected components, weights tied to skill, climatology **floor** on uncertainty | Addresses **overconfidence** and disagreement between sources; intended path for trading-relevant probabilities. |
| **DRN** (`drn.py`) | Neural distributional regression | **Stretch goal** — stubbed; EMOS + M2 delivered the plan’s “calibrated distribution” milestone without PyTorch. |
| **`score.py`** | Gaussian, Gaussian mixture, empirical CDF → **bucket probabilities** | Polymarket contracts are **discrete buckets**; everything downstream needs P(bucket). |
| **Market baseline** (`calibration.py` + `--include-market`) | Latest snapshot mid per bucket, normalized → treat as another “model” | **Honest benchmark**: beating climatology is weak if the **book** already prices the information. Log-loss vs `market:mid` is the right competitive line. |

---

## 6. Strategy layer — translating probabilities into trades

Polymarket’s microstructure matters: **taker fees**, **ticks**, **neg-risk** (11 buckets sum to 1), **maker reward** spread limits.

| Module | Role |
|--------|------|
| `strategy/edge.py` | Fee-aware **expected value** for taker and maker (including a simple reward term). |
| `strategy/negrisk.py` | Coherence of model probs; **flag book violations** (e.g. sum of YES asks off the simplex). |
| `strategy/sizing.py` | **Fractional Kelly** with **hard USD caps** (per bucket, event, day, portfolio). |
| `strategy/taker.py` / `maker.py` | **Adverse-selection padding** by lead time; which buckets to quote as maker. |
| `recommend.py` + `cli/recommend.py` | **Read-only** daily markdown report: edges, sizes, flags — for human review. |

Why: raw model probability is not “edge” until you subtract **fees**, **spread**, and enforce **risk limits**.

---

## 7. Evidence before capital — backtest and paper

| Piece | Purpose |
|-------|---------|
| `backtest.py` + `cli/backtest.py` | Replay **`pm_market_snapshots`** in time order with **no lookahead** on model snapshots; simulate taker/maker fills and PnL. | Shows whether stated edge **survives** fees when history is honest; surfaced M1 tail overconfidence in experiments. |
| `paper.py` + `cli/paper.py` | Persist “would trade” rows to **`paper_trades`**, settle against WU outcomes | Bridges backtest and live: same sizing logic, running on **current** books without signing. |
| `live_report.py` + `cli/live_report.py` | Morning **`reports/live_<date>.md`**: orders, fills, daily_pnl, paper realised vs expected, latest calibration rows | Operational **review loop** before and after enabling automation. |

---

## 8. Automation — where signing lives (and why it is gated)

All **signed** CLOB access for the weather pipeline is concentrated in **`polymarket_weather/automation/order_manager.py`**, using the same **`polymarket_manual.clients`** factory as your manual scripts.

**Gates (by design):**

- `WEATHER_AUTOMATION_ENABLED=1` — master switch; without it, no real orders.
- `WEATHER_KILL_SWITCH` — any non-empty value stops **new** placements (cancel path remains for cleanup).
- **Notional caps** checked against `orders` / `fills` before each placement.

**Orchestrator** (`automation/orchestrator.py` + `cli/run_loop.py`): ingest → predict → recommend → (paper **or** live placement) → refresh `daily_pnl`. Structured logs under `logs/orch_<date>.jsonl` (folder is gitignored).

**Windows scheduling:** `scripts/register_run_loop_task.ps1` registers a **15-minute** one-shot task (no `--loop` in the task; the OS is the supervisor). Use `-DryRun` first to inspect the command line.

**Important:** Live mode is **opt-in** and documented in [PHASE6_GO_LIVE_CHECKLIST.md](PHASE6_GO_LIVE_CHECKLIST.md). No automation flag should be flipped without your explicit review.

---

## 9. Documentation files you should know

| File | Audience |
|------|----------|
| [Agents.md](../Agents.md) | Anyone (human or AI) working in the repo — stack, env vars, CLI table, **safety boundary**. |
| [docs/PHASE6_GO_LIVE_CHECKLIST.md](PHASE6_GO_LIVE_CHECKLIST.md) | You, before enabling `WEATHER_AUTOMATION_ENABLED`. |
| **This file** | You, for a **single read** of scope and rationale. |
| `.env.example` | Template for required keys (never commit `.env`). |

---

## 10. Typical command flow (operator cheat sheet)

```text
python -m polymarket_weather.cli.migrate
python -m polymarket_weather.cli.discover --days-ahead 7
python -m polymarket_weather.cli.ingest_forecasts
python -m polymarket_weather.cli.ingest_ensembles
python -m polymarket_weather.cli.ingest_live_observations
python -m polymarket_weather.cli.fit_postprocess
python -m polymarket_weather.cli.predict
python -m polymarket_weather.cli.calibrate --include-market
python -m polymarket_weather.cli.recommend
python -m polymarket_weather.cli.run_loop --mode paper
python -m polymarket_weather.cli.live_report
```

Adjust flags (`--station`, `--days-ahead`, `--no-ingest`, etc.) per `Agents.md` and `--help`.

---

## 11. Known limitations and honest next steps

- **M1** can show **inflated tail EV**; **M2** needs enough history in `bucket_probs` before `calibrate --model m2_postprocessed_ensemble` has a non-zero sample — treat early paper PnL as **diagnostic**, not proof of edge.
- **Backtest maker fills** are limited by snapshot frequency; live maker performance may differ.
- **Weekly drawdown auto kill-switch** from the plan is not fully automated as a separate watcher — manual `WEATHER_KILL_SWITCH` plus caps is the current safety posture; extend if you want hard automation.
- **Rewards economics** (`rebateRate` interpretation) may need a tiny live probe trade once you are comfortable with caps.

---

## 12. Summary one-liner

**Built:** a Postgres-backed weather + market data stack, calibrated distributional models (through M2/EMOS), fee-aware strategy and reports, historical backtest and paper trading, and an opt-in automation layer with a single signing seam and env gates.  
**Why:** so you can **measure edge against the market and fees**, scale risk slowly, and **never** place USDC by accident until you explicitly enable automation after review.

---

*Generated as part of repository documentation; update this file when major architecture or safety rules change.*
