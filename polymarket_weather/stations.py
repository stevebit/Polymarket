"""Station registry: city slug -> ASOS / GHCN station metadata.

Keep this list in sync with NOAA GHCN-Daily station IDs and the Polymarket
city-slug naming convention used by Gamma (``highest-temperature-in-{slug}-on-...``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class Station:
    slug: str
    polymarket_city_slug: str
    icao: str
    ghcn_id: str
    lat: float
    lon: float
    tz: str
    display_name: str


REGISTRY: Dict[str, Station] = {
    "nyc": Station(
        slug="nyc",
        polymarket_city_slug="nyc",
        icao="KLGA",
        ghcn_id="USW00014732",
        lat=40.7794,
        lon=-73.8803,
        tz="America/New_York",
        display_name="New York City (LaGuardia)",
    ),
    "chicago": Station(
        slug="chicago",
        polymarket_city_slug="chicago",
        icao="KORD",
        ghcn_id="USW00094846",
        lat=41.9742,
        lon=-87.9073,
        tz="America/Chicago",
        display_name="Chicago (O'Hare)",
    ),
    "los-angeles": Station(
        slug="los-angeles",
        polymarket_city_slug="los-angeles",
        icao="KLAX",
        ghcn_id="USW00023174",
        lat=33.9425,
        lon=-118.4081,
        tz="America/Los_Angeles",
        display_name="Los Angeles (LAX)",
    ),
}


def get(slug: str) -> Station:
    try:
        return REGISTRY[slug]
    except KeyError as exc:
        raise KeyError(
            f"Unknown station slug {slug!r}. Known: {sorted(REGISTRY)}"
        ) from exc


def all_stations() -> list[Station]:
    return list(REGISTRY.values())
