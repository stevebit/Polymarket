from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    host: str
    chain_id: int
    private_key: str | None
    signature_type: int
    funder_address: str | None


def _strip_optional_hex(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def load_settings() -> Settings:
    host = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com").strip()
    chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
    private_key = _strip_optional_hex(os.getenv("POLYMARKET_PRIVATE_KEY"))
    signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
    funder = _strip_optional_hex(os.getenv("POLYMARKET_FUNDER_ADDRESS"))
    return Settings(
        host=host,
        chain_id=chain_id,
        private_key=private_key,
        signature_type=signature_type,
        funder_address=funder,
    )


def require_private_key(settings: Settings) -> str:
    if not settings.private_key:
        raise SystemExit(
            "Missing POLYMARKET_PRIVATE_KEY in environment (.env). "
            "Authenticated scripts cannot run without it."
        )
    return settings.private_key
