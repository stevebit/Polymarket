# Agent notes — Polymarket manual trading

This repo is a **small Python toolkit** for interacting with the Polymarket **CLOB** from the command line. Trading is **manual**: scripts require explicit confirmation flags for live orders; there is no bot loop.

## Stack

- **Python 3.10+**, `python-dotenv`, editable install: `pip install -e .`
- **Always use `py-clob-client-v2`** (`from py_clob_client_v2...`). Do **not** switch back to `py-clob-client` (v1): **email / Magic (`POLYMARKET_SIGNATURE_TYPE=1`) orders fail** on v1 with `order_version_mismatch` after the 2026 CLOB upgrade.
- Shared factories live in `polymarket_manual/clients.py` and `polymarket_manual/config.py`.

## Secrets and environment

- Copy `.env.example` → `.env`. **Never commit `.env`** (already gitignored).
- Required for authenticated scripts: `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_SIGNATURE_TYPE`, and for types `1` / `2`: `POLYMARKET_FUNDER_ADDRESS` (Polygon address that holds the Polymarket balance).
- Optional: `POLYMARKET_HOST` (default `https://clob.polymarket.com`), `POLYMARKET_CHAIN_ID` (default `137`).

## Scripts (repo root)

| Script | Purpose |
|--------|---------|
| `scripts/ping.py` | CLOB health (no auth). |
| `scripts/list_markets.py` | Paginated market listing (`--max-pages`, `--detailed`, filters). |
| `scripts/orderbook.py` | Read-only book for a `--token-id`. |
| `scripts/list_orders.py` | Open orders (auth). |
| `scripts/place_limit.py` | GTC limit order; requires `--confirm YES` to post (or `--dry-run`). |
| `scripts/place_market.py` | Dollar BUY / share SELL market order (FOK/FAK); `--confirm YES` to post. |
| `scripts/cancel_order.py` | Cancel by `--order-id`; requires `--confirm YES`. |

## Market / token discovery

- **Gamma API** (e.g. `https://gamma-api.polymarket.com/events?slug=...`) maps UI slugs to `clobTokenIds` and `conditionId`. A Polymarket **event** has multiple **markets** (e.g. “released by May 22” vs “released by June 30” are different contracts — match the user’s words to the right `question` / slug).
- Outcomes are binary Yes/No per market; **Yes** is typically the **first** entry in `clobTokenIds` when `outcomes` is `["Yes","No"]` — verify against Gamma JSON before large trades.

## Safety and scope

- Respect the existing **confirmation gates**; do not bypass `--confirm YES` for production flows.
- Do not expand scope into automated strategies unless the user explicitly asks.
- This is **not** legal or investment advice; prediction markets vary by jurisdiction.

## Docs

- Upstream client: [Polymarket/py-clob-client](https://github.com/Polymarket/py-clob-client) — use the **v2** PyPI package `py-clob-client-v2` as pinned in `pyproject.toml`.

## Weather research package (`polymarket_weather/`)

A package for ingesting daily-temperature event data into Azure Postgres,
fitting calibrated distributional models, generating fee-aware bucket-level
recommendations, paper-trading them, and (only behind explicit env gates)
placing live signed orders.

The original research plan lives at
`.cursor/plans/weather_temperature_betting_plan_*.plan.md`; the profitable-
trading plan lives at
`.cursor/plans/profitable_weather_betting_plan_*.plan.md`.

Most of the package is **read-only**. The single signed-flow seam is
`polymarket_weather/automation/`, gated by `WEATHER_AUTOMATION_ENABLED=1`
and the `WEATHER_KILL_SWITCH` env var (see "Safety boundary" below). Any
agent that touches `polymarket_weather/automation/` must keep both gates
intact.

The Phase 6 pre-flight review (must be passed before flipping live) lives
at [docs/PHASE6_GO_LIVE_CHECKLIST.md](docs/PHASE6_GO_LIVE_CHECKLIST.md).

For a **narrative overview** of what was built and why, see
[docs/OVERVIEW_REPORT.md](docs/OVERVIEW_REPORT.md).

For **backtest mechanics**, snapshot subsampling, JSON export, and the
**hourly / neighbor-station** ingest path, see
[docs/BACKTEST_AND_HIGH_RES_DATA.md](docs/BACKTEST_AND_HIGH_RES_DATA.md).

To **copy Azure Postgres into a local Windows Postgres** instance for browsing
with pgAdmin/DBeaver, see
[docs/LOCAL_POSTGRES_SETUP.md](docs/LOCAL_POSTGRES_SETUP.md) and
`scripts/sync_azure_weather_to_local.ps1`.

Future agents: do not enable `WEATHER_AUTOMATION_ENABLED=1` on the
user's behalf — that is a manual decision after the checklist is green.

### Environment keys (in addition to `POLYMARKET_*`)

| Key | Purpose |
|-----|---------|
| `WEATHER_POSTGRES_URL` | Azure Postgres flexible-server libpq URL with `sslmode=require`. Password is URL-encoded. |
| `NOAA_Token_ID` | NOAA Climate Data Online v2 token (note the unusual casing — used as-is). Request at [ncdc.noaa.gov/cdo-web/token](https://www.ncdc.noaa.gov/cdo-web/token). |
| `WEATHER_STATIONS` | Comma-separated station slugs. If unset or blank, defaults to all **11 US** Polymarket daily-temperature cities (`atlanta`, `austin`, `chicago`, `dallas`, `denver`, `houston`, `los-angeles`, `miami`, `nyc`, `san-francisco`, `seattle`). |
| `WEATHER_HTTP_UA` | User-Agent string for outbound HTTP (NWS API requires identification). |
| `WEATHER_AUTOMATION_ENABLED` | Must equal `1` for `polymarket_weather/automation/order_manager.py` to actually sign and submit orders. Anything else (unset, `0`, `false`, etc.) keeps the automation in read-only/paper mode. |
| `WEATHER_KILL_SWITCH` | If **set to anything**, the order manager refuses to place or replace orders even when `WEATHER_AUTOMATION_ENABLED=1`. Use to halt live trading without redeploying. |

Connection string format (do not commit, do not log):

```
postgresql://weatheradmin:<urlencoded-pw>@<server>.postgres.database.azure.com:5432/weather?sslmode=require
```

### Schema and migrations

- SQL files live in `migrations/` (currently `001_init.sql` →
  `005_hourly_observations.sql`).
- `polymarket_weather/db.py` discovers and applies any unapplied numbered
  files in a transaction each, recording state in `schema_migrations`.
- Tables: `stations`, `pm_events`, `pm_buckets`, `pm_market_snapshots`,
  `forecasts`, `forecast_members`, `hourly_observations`, `observations`, `climatology`,
  `predictions`, `bucket_probs`, `calibration_runs`, `postprocess_coefs`,
  `paper_trades`, `orders`, `fills`, `daily_pnl`. All upserts are idempotent.
- Stations are seeded from `polymarket_weather/stations.py` on first run.

- On a **new** database, run `python -m polymarket_weather.cli.migrate` before any other weather CLI so tables and `schema_migrations` exist.

- Calibration output under `reports/` (markdown and PNG) is **gitignored**; generate locally as needed.

### CLIs (run with `python -m polymarket_weather.cli.<name>`)

| Module | Purpose |
|--------|---------|
| `migrate` | Apply pending migrations + seed station registry. |
| `discover` | Gamma event discovery + bucket parsing + read-only CLOB book snapshots. |
| `ingest_forecasts` | Open-Meteo (multi-model + bestmatch) + NWS hourly + NWS daily + NBM v5.0 station bulletins. |
| `ingest_ensembles` | Open-Meteo ensemble member traces (GFS-ENS / IFS-ENS / GEM-ENS) into `forecast_members`. |
| `ingest_live_observations` | NWS station observations + Iowa Mesonet ASOS for today-so-far feature. |
| `ingest_hourly_observations` | Iowa Mesonet ASOS **hourly** backfill into `hourly_observations` for primary + optional **neighbor** ICAOs per city (`NEIGHBOR_ICAOS_BY_SLUG`). |
| `ingest_observations` | NOAA GHCN-Daily TMAX backfill / incremental. |
| `ingest_resolution_observations` | Weather Underground/weather.com historical Tmax ingest for Polymarket resolution-source parity checks. |
| `refresh_climatology` | Per-station day-of-year TMAX climatology. |
| `fit_postprocess` | Fit EMOS coefficients per (station, source, lead_day); writes to `postprocess_coefs`. |
| `predict` | M0 best-source + M1 ensemble Gaussian + M2 EMOS-weighted Gaussian-mixture models, written to `predictions` and `bucket_probs`. |
| `calibrate` | Backtest log-loss / Brier / reliability over resolved events; writes `reports/calibration_<run_id>.md` and a `calibration_runs` row. `--include-market` adds the market-implied baseline. |
| `parity_report` | NOAA vs WU source-parity diagnostics over overlapping station/day observations; writes `reports/parity_<timestamp>.md`. |
| `recommend` | **Read-only** daily fee-aware recommendations: per bucket, taker / maker EV, neg-risk projection, fractional-Kelly size against tiny-bankroll caps. Writes `reports/recommendations_<date>.md`. |
| `backtest` | Replay `pm_market_snapshots` chronologically with leakage-controlled fills + maker-through-book model; writes `reports/backtest_<run>.md`. Supports `--take-every-n-snapshots`, `--export-json`, equity curve + snapshot spacing stats in the JSON. |
| `backtest_dashboard` | Static Plotly HTML from `--export-json` output (equity, PnL by station/lead, fill table). |
| `paper` | `submit` writes recommendations to `paper_trades`, `settle` marks resolved trades against WU outcomes, `summary` reports realised PnL / fees / hit rate. |
| `run_loop` | Orchestrator: ingest delta → predict → recommend → reconcile → place/paper. `--mode paper` is read-only; `--mode live` is gated by automation envs (see Safety boundary). |
| `live_report` | **Read-only** morning review: open orders, fills, daily PnL, paper-trade realised/expected ratio, latest calibration per model, open exposure by event, plus the Phase 6 review checklist. Writes `reports/live_<date>.md`. |
| `dashboard` | Standalone HTML dashboard: US station map + station-click time series from raw observations and forecasts. |

Most modules accept `--station` (comma-separated) and `--verbose`; flags such as `--date`, `--days-ahead`, `--past-days`, and `--lookback-days` vary — run `python -m polymarket_weather.cli.<name> --help`.

Of these, `run_loop --mode live` is the **only** entrypoint that can sign
or place orders. All other CLIs are strictly read-only.

### Safety boundary

Read-only by default; signed flow lives in **one** narrow seam.

1. **Read-only modules.** All of `polymarket_weather/data/`, `models/`,
   `score.py`, `calibration.py`, `recommend.py`, `paper.py`, `backtest.py`,
   `markets.py`, and every CLI under `polymarket_weather/cli/` **except**
   `run_loop.py` are strictly read-only against Polymarket's public Gamma +
   CLOB endpoints (no API key, no signing). They never modify
   `polymarket_manual/` or `scripts/`.
2. **Signed seam.** `polymarket_weather/automation/order_manager.py` is the
   **only** module in this package allowed to construct a v2 ClobClient with
   credentials and call signed methods (`create_order`, `post_order`,
   `cancel_order`). It reuses the `polymarket_manual.clients.make_trading_client`
   factory so the whole repo has one signing surface.
3. **Hard env gates.** `OrderManager` refuses to place / replace orders
   unless **both** of these are true at run time:
   - `WEATHER_AUTOMATION_ENABLED=1`
   - `WEATHER_KILL_SWITCH` is unset / empty.
   Setting `WEATHER_KILL_SWITCH=1` immediately halts signed flow on the
   next tick without touching code or task scheduling.
4. **Hard cap gates.** Every `place_limit` is also gated by
   `polymarket_weather/strategy/sizing.py` caps (per-bucket, per-event,
   per-day, per-portfolio notional) using actual filled+open notional from
   the `orders` and `fills` tables. Cap breaches raise `CapBreach` and are
   logged but do not stop the rest of the tick.
5. **Manual scripts unchanged.** The `polymarket_manual/scripts/*.py`
   confirmation gates (`--confirm YES`) are untouched and remain the only
   path for ad-hoc human trades. The orchestrator does not import them.

Future agents working in `polymarket_weather/automation/`:

- Do not remove or weaken the `WEATHER_AUTOMATION_ENABLED` /
  `WEATHER_KILL_SWITCH` checks.
- Do not add a second signing surface elsewhere in the weather package.
- Do not bypass the caps in `OrderManager._check_caps`.
- Do not auto-flip `WEATHER_AUTOMATION_ENABLED` on the user's behalf;
  scaling the bankroll, enabling live mode, and editing caps are all
  user-initiated decisions.

### Filename note (Windows)

On case-insensitive filesystems, `Agents.md` and `AGENTS.md` resolve to the same path; keep this file as **`Agents.md`** as the single canonical agent notes file.
