# Backtesting environment and higher-resolution weather data

This note ties together **why** the backtest works the way it does, **how** to
stress-test it, and **how** hourly / multi-site observations fit into the
longer-term modeling roadmap.

## 1. What the backtest simulates

The engine in `polymarket_weather/backtest.py` replays **`pm_market_snapshots`**
in chronological order. For each snapshot time it:

1. Pulls the latest **`bucket_probs`** for your chosen `model_id` with
   `run_time <= snapshot_at` (no look-ahead).
2. Builds top-of-book YES levels per bucket and derives NO via neg-risk
   consistency (`recommend._no_book_from_yes`).
3. Applies **fee-aware** taker/maker EV and **Kelly + caps** (per-event usage
   is tracked; per-day / portfolio caps are intentionally relaxed in replay —
   see module docstring).
4. **Taker** fills immediately at the visible price; **maker** orders fill when
   a *later* snapshot shows the book traded through your limit (coarse
   approximation — real fill probability depends on queue position and
   snapshot cadence).

Settlement uses **daily** resolved TMAX from `observations`, preferring
`wunderground:historical` — same resolution definition as Polymarket.

### Improvements shipped in this iteration

| Feature | Why it matters |
|--------|----------------|
| **`station_slug` on every fill** | Correct **by-station** PnL rollups without parsing `event_slug` strings. |
| **`snapshot_stats`** | Median hours between *used* snapshots — explains sparse maker fills when cadence is hours. |
| **`--take-every-n-snapshots`** | Downsample replay to mimic “we only trade on hourly book updates” or to speed up long windows. |
| **`equity_curve` + `max_drawdown_usd`** | Ordered by fill time; gross PnL path (fees still in `fees_paid_usd`). |
| **`--export-json` + `backtest_dashboard`** | One JSON artifact → static Plotly HTML for review before go-live. |

**Workflow**

```powershell
python -m polymarket_weather.cli.backtest --start 2025-01-01 --end 2025-06-01 `
  --model m2_postprocessed_ensemble --strategy both `
  --export-json reports/backtest_run.json

python -m polymarket_weather.cli.backtest_dashboard --from-json reports/backtest_run.json `
  --output reports/backtest_run.html
```

The single-run dashboard shows equity (gross + net + drawdown), a reliability
diagram on the **acted-on subset only**, an EV-vs-realised per-share scatter
with a fit line, a station × lead net-PnL heatmap, a tail-vs-centre hit-rate
bar, fill-rate / coverage tiles, top winners / losers by event, and a recent-
fills table with realised labels.

To **compare two or more runs** (e.g. M1 vs M2 with the same window), pass
``--from-json`` repeatedly:

```powershell
python -m polymarket_weather.cli.backtest --start 2025-01-01 --end 2025-06-01 `
  --model m1_ensemble_gaussian --export-json reports/m1.json
python -m polymarket_weather.cli.backtest --start 2025-01-01 --end 2025-06-01 `
  --model m2_postprocessed_ensemble --export-json reports/m2.json

python -m polymarket_weather.cli.backtest_dashboard `
  --from-json reports/m1.json --label "M1" `
  --from-json reports/m2.json --label "M2" `
  --output reports/compare.html
```

The compare dashboard overlays net equity curves and reliability diagrams,
plus grouped per-station and per-lead bar charts so you can see *where* one
model's edge differs from the other.

## 2. Daily max vs higher frequency

Polymarket contracts still pay on **one number per local calendar day**: the
official daily high at the resolution station. Sub-daily data does **not**
replace that label — it **informs** short-lead predictive distributions (e.g.
“it is already 88°F at 10am local; the remaining uplift distribution is
narrower”).

## 3. `hourly_observations` + neighbor airports

Migration **`005_hourly_observations.sql`** adds table `hourly_observations`:

- `station_id` — always the **canonical** Polymarket market station row.
- `site_icao` — the METAR reporting site (`KLGA`, or a neighbor like `KJFK`).
- `obs_ts_utc`, `temp_f`, Mesonet `source`.

**Neighbor list:** `polymarket_weather/stations.NEIGHBOR_ICAOS_BY_SLUG` maps each
US city slug to extra ICAOs in the same metro. Ingest stores those rows under
the **parent** `station_id` so you can aggregate “metro max” or spatial spread
features without duplicating station registry rows.

**Ingest**

```powershell
python -m polymarket_weather.cli.migrate
python -m polymarket_weather.cli.ingest_hourly_observations `
  --start 2024-01-01 --end 2024-12-31 --station nyc,chicago
```

Mesonet requests are chunked (default 120 days) to reduce timeouts. Respect
their rate limits; the async driver sleeps briefly between chunks.

## 4. Retraining models with richer inputs (roadmap)

Today **EMOS** (`models/postprocess.py`) and **M2** consume daily forecast means
/ spreads and **daily** verification. To fold in hourly / neighbor data:

1. **Features** — For each `(station_id, target_date, lead_day)` add optional
   scalars, e.g. `metro_neighbor_spread_f`, `running_max_primary_f_at_12z`, etc.,
   computed from `hourly_observations` in local time (use `stations.REGISTRY[].tz`).
2. **Training target** — Still daily TMAX at resolution (WU / GHCN).
3. **Model choice** — Extend EMOS with extra regressors, or activate the **DRN**
   stub (`models/drn.py`) once you want nonlinear interactions.

Keep each step **backtested** with the same `bucket_probs` leakage rules: any
feature available at `snapshot_at` must be computable from data with timestamp
≤ `snapshot_at`.

## 5. Honest limitations

- **Maker fills** remain sensitive to snapshot spacing; use `snapshot_stats`
  and subsampling flags to sanity-check.
- **Hourly history** from Mesonet is convenient but not identical to ASOS QC
  used in all NWS products — compare to `asos:live` for spot checks.
- **Multi-site** neighbors improve *context*; they do not change Polymarket’s
  single-station resolution — do not double-count them as separate “truth”
  labels.

---

When in doubt, run **`calibrate --include-market`** and a **paper** `run_loop`
tick before trusting any backtest PnL in production sizing.
