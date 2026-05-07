# Polymarket manual trading (Python)

Small helpers around [Polymarket/py-clob-client-v2](https://github.com/Polymarket/py-clob-client) (the v2 PyPI package) for **read-only checks** and **explicitly confirmed** orders. v2 is required for reliable order posting with **email / Magic** accounts (v1 hits `order_version_mismatch` as of the 2026 CLOB upgrade).

## Setup

1. Install Python 3.10+.
2. Create a virtual environment and install in editable mode:

```powershell
cd c:\Users\steve\Git_Code\Polymarket
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

3. Copy `.env.example` to `.env` and set variables for how you log in (see upstream [py-clob-client README](https://github.com/Polymarket/py-clob-client/blob/main/README.md)).

   **Email / Magic (“Sign in with email”)** — set all three:

   - `POLYMARKET_PRIVATE_KEY` — the signing key you use for API access (from Polymarket’s export or builder tooling for your account).
   - `POLYMARKET_SIGNATURE_TYPE=1`
   - `POLYMARKET_FUNDER_ADDRESS` — your **funding** Polygon address (`0x…`): the wallet the site treats as holding your balance (profile / wallet / deposit). The CLOB needs this because the signing key and the funded address are not the same for Magic/email flows.

4. **EOA / MetaMask users:** use `POLYMARKET_SIGNATURE_TYPE=0`, leave `POLYMARKET_FUNDER_ADDRESS` empty unless your setup notes say otherwise, and set USDC + conditional token allowances once (Polymarket documents this; email wallets usually skip allowances).

## Scripts

- `python scripts/ping.py` — CLOB health (no secrets).
- `python scripts/list_markets.py` — walk **paginated** market lists from the CLOB (`--detailed` includes `enable_order_book`; use `--max-pages` as a safety cap).
- `python scripts/orderbook.py --token-id <id>` — midpoint and top of book (no secrets).
- `python scripts/list_orders.py` — your open orders (needs `.env`).
- `python scripts/place_limit.py ...` — posts a limit order only if you pass `--confirm YES` after reviewing the printed summary.
- `python scripts/cancel_order.py --order-id <id> --confirm YES` — cancel one order.

**Token IDs** come from Polymarket’s Gamma/markets API ([docs](https://docs.polymarket.com/developers/gamma-markets-api/get-markets)).

## Safety

There is no background bot. Anything that moves money or posts orders requires an explicit `--confirm YES` flag so a mistyped command cannot fire silently.

## Weather research package

A separate package, `polymarket_weather/`, provides a **read-only** research
toolkit for Polymarket daily-temperature events (NYC / Chicago / LA today,
extensible). It pulls multi-model forecasts (Open-Meteo, NWS), NOAA GHCN-D
observations, and Polymarket bucket probabilities into Azure Postgres, fits
two baseline distributional models, and writes a calibration report to
`reports/`. **It stops at calibration** — no order placement, no bet sizing,
no scheduler.

### Setup

1. Provision an Azure Postgres flexible server and a `weather` database.
2. Add to `.env` (see `.env.example`):
   - `WEATHER_POSTGRES_URL` (libpq URL, password URL-encoded, `sslmode=require`)
   - `NOAA_Token_ID` (NOAA Climate Data Online token; note the casing — [request token](https://www.ncdc.noaa.gov/cdo-web/token))
   - `WEATHER_STATIONS=nyc,chicago,los-angeles`
   - `WEATHER_HTTP_UA=polymarket-weather (contact: <your-email>)`
3. `pip install -e .` — installs `psycopg`, `httpx`, `pandas`, `numpy`,
   `matplotlib`, `scipy` alongside the existing CLOB deps.

### Daily flow

```powershell
python -m polymarket_weather.cli.migrate
python -m polymarket_weather.cli.discover --days-ahead 7
python -m polymarket_weather.cli.ingest_forecasts --past-days 7 --forecast-days 8
python -m polymarket_weather.cli.ingest_observations --days 30
python -m polymarket_weather.cli.refresh_climatology
python -m polymarket_weather.cli.predict --days-ahead 7
python -m polymarket_weather.cli.calibrate --lookback-days 30
```

The calibration report (markdown + reliability PNG) lands under `reports/`,
which is gitignored. Re-running any CLI is idempotent — every write goes
through `INSERT ... ON CONFLICT DO UPDATE`. See `Agents.md` for the full
schema, env-key reference, and safety boundaries.
