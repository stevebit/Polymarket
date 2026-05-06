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
