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
