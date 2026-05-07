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

A separate, **read-only** package for ingesting daily-temperature event data
into Azure Postgres, fitting baseline distributional models, and reporting
calibration metrics. **Stops at calibration**: no signing, no order placement,
no scheduler, no imports from `polymarket_manual.clients`. The original
plan lives at `.cursor/plans/weather_temperature_betting_plan_*.plan.md`.

### Environment keys (in addition to `POLYMARKET_*`)

| Key | Purpose |
|-----|---------|
| `WEATHER_POSTGRES_URL` | Azure Postgres flexible-server libpq URL with `sslmode=require`. Password is URL-encoded. |
| `NOAA_Token_ID` | NOAA Climate Data Online v2 token (note the unusual casing — used as-is). Request at [ncdc.noaa.gov/cdo-web/token](https://www.ncdc.noaa.gov/cdo-web/token). |
| `WEATHER_STATIONS` | Comma-separated station slugs (default `nyc,chicago,los-angeles`). |
| `WEATHER_HTTP_UA` | User-Agent string for outbound HTTP (NWS API requires identification). |

Connection string format (do not commit, do not log):

```
postgresql://weatheradmin:<urlencoded-pw>@<server>.postgres.database.azure.com:5432/weather?sslmode=require
```

### Schema and migrations

- SQL files live in `migrations/` (currently `001_init.sql`).
- `polymarket_weather/db.py` discovers and applies any unapplied numbered
  files in a transaction each, recording state in `schema_migrations`.
- Tables: `stations`, `pm_events`, `pm_buckets`, `pm_market_snapshots`,
  `forecasts`, `observations`, `climatology`, `predictions`, `bucket_probs`,
  `calibration_runs`. All upserts are idempotent.
- Stations are seeded from `polymarket_weather/stations.py` on first run.

- On a **new** database, run `python -m polymarket_weather.cli.migrate` before any other weather CLI so tables and `schema_migrations` exist.

- Calibration output under `reports/` (markdown and PNG) is **gitignored**; generate locally as needed.

### CLIs (run with `python -m polymarket_weather.cli.<name>`)

| Module | Purpose |
|--------|---------|
| `migrate` | Apply pending migrations + seed station registry. |
| `discover` | Gamma event discovery + bucket parsing + read-only CLOB book snapshots. |
| `ingest_forecasts` | Open-Meteo (multi-model) + NWS gridpoint hourly. |
| `ingest_observations` | NOAA GHCN-Daily TMAX backfill / incremental. |
| `refresh_climatology` | Per-station day-of-year TMAX climatology. |
| `predict` | M0 best-source + M1 ensemble Gaussian models, written to `predictions` and `bucket_probs`. |
| `calibrate` | Backtest log-loss / Brier / reliability over resolved events; writes `reports/calibration_<run_id>.md` and a `calibration_runs` row. |

Most modules accept `--station` (comma-separated) and `--verbose`; flags such as `--date`, `--days-ahead`, `--past-days`, and `--lookback-days` vary — run `python -m polymarket_weather.cli.<name> --help`.
None of them place orders or sign anything.

### Safety boundary

The weather package only depends on Polymarket's **public** Gamma + read-only
CLOB endpoints (no API key, no signing). It does not modify
`polymarket_manual/` or `scripts/`. Never extend it into bet sizing or order
placement without an explicit user request — order paths must continue to go
through the existing `--confirm YES`-gated `polymarket_manual` scripts.

### Filename note (Windows)

On case-insensitive filesystems, `Agents.md` and `AGENTS.md` resolve to the same path; keep this file as **`Agents.md`** as the single canonical agent notes file.
