"""List your open CLOB orders (requires .env credentials)."""

import json

from py_clob_client_v2.clob_types import OpenOrderParams

from polymarket_manual.clients import make_trading_client
from polymarket_manual.config import load_settings


def main() -> None:
    settings = load_settings()
    client = make_trading_client(settings)
    orders = client.get_open_orders(OpenOrderParams())
    print(json.dumps(orders, indent=2, default=str))


if __name__ == "__main__":
    main()
