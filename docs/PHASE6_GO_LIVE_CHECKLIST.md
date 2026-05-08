# Phase 6 — Pre-flight review (go-live checklist)

This document is the single place to look before flipping
`WEATHER_AUTOMATION_ENABLED=1`. The orchestrator is fully wired in paper
mode but **no real money has been put at risk** — the env gate is unset
and the Windows scheduled task is not registered. Read this file end to
end, fix any item that doesn't pass, then flip both gates yourself.

## Hard gates that already exist in code

| Gate | Where | Effect when missing/triggered |
|------|-------|-------------------------------|
| `WEATHER_AUTOMATION_ENABLED=1` | `polymarket_weather/automation/order_manager.py` | `OrderManager.place_limit` raises `OrderManagerDisabled`. Orchestrator silently falls back to `paper` mode and logs a loud warning. |
| `WEATHER_KILL_SWITCH` (any value) | same | `place_limit` raises `OrderManagerKilled`; cancels still work so we can clean up open orders. |
| Per-bucket / per-event / per-day / per-portfolio notional caps | `OrderManager._check_caps` reading from `orders` + `fills` | `CapBreach` raised, that single placement skipped, orchestrator continues. |
| Min edge per dollar of notional after fees | `polymarket_weather/strategy/edge.py` | Bucket dropped from `recommend.py` output before sizing runs. |
| Min Kelly fraction + min order shares | `polymarket_weather/strategy/sizing.py` | Bucket dropped from sizing if Kelly says size = 0 or shares < 5. |

You should not weaken any of these without an explicit reason.

## Pre-flight checks (must all pass)

Run these in order. Each item has the exact command to verify.

### 1. Migrations are up to date

```powershell
.\.venv\Scripts\python.exe -m polymarket_weather.cli.migrate
```

Expected: `Schema OK. Row counts: …`. The four migrations that must be
applied are `001_init`, `002_ensembles_live`, `003_paper_trades`,
`004_orders_fills`.

### 2. Orchestrator runs cleanly in paper mode

```powershell
.\.venv\Scripts\python.exe -m polymarket_weather.cli.run_loop --mode paper --no-ingest --no-snapshot --days-ahead 1
```

Expected (last line):

```
mode=paper fallback_paper=False snapshots=… preds=… bucket_probs=… paper_orders=… placed=0 cap_breaches=0 errors=0
```

`errors=0` and `placed=0` are non-negotiable. Any other count is fine
for paper mode.

### 3. Fresh recommendations report looks sensible

```powershell
.\.venv\Scripts\python.exe -m polymarket_weather.cli.recommend --days-ahead 1
```

Open `reports/recommendations_<date>.md` and confirm:

- per-event sizes are within the tiny-bankroll caps (`≤ $5` per bucket,
  `≤ $20` per event),
- no bucket shows EV that is "too good to be true" (e.g. > 50¢ EV per
  share). M1 has a known overconfidence in tail buckets — if M2 isn't
  yet calibrated for the city in question, expect M1 fallback EV to be
  inflated. Lower the per-bucket cap or wait for more M2 calibration
  before going live.
- neg-risk arb flags in the report match the actual book.

### 4. Backtest doesn't show systematic loss for the chosen model

```powershell
.\.venv\Scripts\python.exe -m polymarket_weather.cli.backtest --model m2_postprocessed_ensemble --strategy taker
```

Expected: cumulative PnL ≥ 0 over the resolved window, max drawdown
< 25% of bankroll. **If you see a large negative PnL like the M1 baseline
backtest, do not go live with that model.**

### 5. Calibration: at least one model beats `market:mid`

```powershell
.\.venv\Scripts\python.exe -m polymarket_weather.cli.calibrate --include-market --lookback-days 90
```

In the comparison table at the bottom, at least one of `m0_best_source`,
`m1_ensemble_gaussian`, `m2_postprocessed_ensemble` must have a lower
log-loss than `market:mid` on the same sample. Otherwise the book is
already pricing every edge we have, and you should stay in paper or quote
maker-only with a wide spread.

### 6. M2 has a non-empty calibration row

The first run of `calibrate --model m2_postprocessed_ensemble` gives
`sample_n=0` because we just started writing M2 predictions. Wait until
at least one resolved event has a M2 prediction in `bucket_probs`, then
rerun calibration. The live report flags this for you in the calibration
table.

### 7. Paper trades realised vs expected is healthy

```powershell
.\.venv\Scripts\python.exe -m polymarket_weather.cli.paper settle
.\.venv\Scripts\python.exe -m polymarket_weather.cli.live_report --paper-lookback-days 30
```

Open `reports/live_<date>.md` → "Paper trades settled in last N rows".
The `realised / expected ratio` should be ≥ 0.5 over ≥ 30 settled days
before you flip live. A ratio < 0.5 means the model EV overstates real
edge by more than 2x — push that down via better caps or a wider min
edge before risking USDC.

### 8. Polymarket fee schedule hasn't changed

Open `pm_market_snapshots` and check the latest
`raw->'feeSchedule'` for any of the 11 events. The system assumes
`{rate: 0.05, takerOnly: true, rebateRate: 0.25}`. If Polymarket changed
this, update `polymarket_weather/strategy/edge.py:DEFAULT_TAKER_FEE`
before going live.

### 9. CLOB credentials work

```powershell
.\.venv\Scripts\python.exe scripts\ping.py
.\.venv\Scripts\python.exe scripts\list_orders.py
```

Both must succeed. `list_orders.py` exercises the same signing path the
order manager uses; if it fails, the orchestrator will too.

## Going live (only after all of the above pass)

1. Set the env var in your **user** scope (not the repo `.env`):

   ```powershell
   [Environment]::SetEnvironmentVariable("WEATHER_AUTOMATION_ENABLED", "1", "User")
   ```

   Open a fresh shell so the variable is visible.

2. Register the scheduled task with tiny-bankroll caps:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\register_run_loop_task.ps1 `
       -Mode live -PerBucketCap 5 -PerEventCap 20 -PerDayCap 100
   ```

   The default trigger fires every 15 minutes. Add `-DryRun` first to see
   the resulting command without registering.

3. Monitor the **first** scheduled tick:

   ```powershell
   Start-ScheduledTask -TaskName PolymarketWeatherOrchestrator
   Get-Content -Wait logs\orch_$(Get-Date -Format yyyy-MM-dd).jsonl
   ```

4. Read `reports/live_<date>.md` every morning. If the realised/expected
   ratio drops below 0.5, log-loss spikes, or any error count is non-zero
   for two ticks in a row, set the kill switch:

   ```powershell
   [Environment]::SetEnvironmentVariable("WEATHER_KILL_SWITCH", "1", "User")
   ```

   The next tick will refuse to place new orders. Cancel-only path stays
   open so you can clean up.

## Stopping the system

To pause without deregistering the task:

```powershell
[Environment]::SetEnvironmentVariable("WEATHER_KILL_SWITCH", "1", "User")
```

To stop completely:

```powershell
Unregister-ScheduledTask -TaskName PolymarketWeatherOrchestrator -Confirm:$false
[Environment]::SetEnvironmentVariable("WEATHER_AUTOMATION_ENABLED", $null, "User")
```

Both leave the paper-trade and reporting paths fully functional.
