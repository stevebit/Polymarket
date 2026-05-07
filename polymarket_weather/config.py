"""Environment / configuration loader for the weather package.

Reads from a project-root ``.env`` (via ``python-dotenv``) and exposes typed
accessors. All values are lazy and re-read on demand so tests can monkeypatch
``os.environ`` without restarting the process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")


def _required(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            "See .env.example for the full list of weather keys."
        )
    return val


def postgres_url() -> str:
    """libpq URL for the Azure Postgres weather database."""
    return _required("WEATHER_POSTGRES_URL")


def noaa_token() -> str:
    """NOAA CDO v2 API token. Variable name uses the user's existing casing."""
    return _required("NOAA_Token_ID")


def http_user_agent() -> str:
    return os.environ.get(
        "WEATHER_HTTP_UA",
        "polymarket-weather (contact: unset)",
    )


def station_slugs() -> List[str]:
    raw = os.environ.get("WEATHER_STATIONS", "nyc,chicago,los-angeles")
    return [s.strip() for s in raw.split(",") if s.strip()]


@dataclass(frozen=True)
class Paths:
    repo_root: Path
    migrations: Path
    reports: Path


def paths() -> Paths:
    return Paths(
        repo_root=_REPO_ROOT,
        migrations=_REPO_ROOT / "migrations",
        reports=_REPO_ROOT / "reports",
    )
