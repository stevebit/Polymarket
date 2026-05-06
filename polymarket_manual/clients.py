from __future__ import annotations

from py_clob_client_v2.client import ClobClient

from polymarket_manual.config import Settings, require_private_key


def make_readonly_client(settings: Settings) -> ClobClient:
    """Level 0 client: market data only, no wallet."""
    return ClobClient(settings.host.rstrip("/"), settings.chain_id)


def make_trading_client(settings: Settings) -> ClobClient:
    """Authenticated client for orders and private endpoints (v2 API; required for email/Magic)."""
    key = require_private_key(settings)
    if settings.signature_type in (1, 2) and not settings.funder_address:
        raise SystemExit(
            "POLYMARKET_FUNDER_ADDRESS is required when POLYMARKET_SIGNATURE_TYPE is 1 (email/Magic) "
            "or 2 (browser proxy). Set it to the Polygon 0x address that holds your Polymarket balance "
            "(same address the site shows for your wallet / deposits)."
        )
    client = ClobClient(
        settings.host.rstrip("/"),
        settings.chain_id,
        key=key,
        signature_type=settings.signature_type,
        funder=settings.funder_address,
    )
    client.set_api_creds(client.create_or_derive_api_key())
    return client
