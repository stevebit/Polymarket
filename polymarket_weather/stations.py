"""Station registry: internal slug -> ASOS / GHCN metadata.

Slugs and ``polymarket_city_slug`` match Polymarket Gamma event URLs, e.g.
``highest-temperature-in-dallas-on-may-8-2026``.

**US daily high-temperature markets** (Polymarket tag ``daily-temperature`` /
``highest-temperature``, closed=false snapshot) currently list 51 global cities;
this registry includes the **11 United States** locations only. ICAO and
``ghcn_id`` are chosen to align with each market's published resolution station
(Wunderground airport names in the event description) so GHCN-D and NWS
gridpoint data track the same site where possible.

To add international cities later, extend ``_STATIONS`` and keep
``polymarket_city_slug`` identical to the Gamma slug segment after
``highest-temperature-in-``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Final, FrozenSet, Tuple


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


# (slug, polymarket_city_slug, icao, ghcn_id, lat, lon, tz, display_name)
_STATIONS: tuple[tuple[str, str, str, str, float, float, str, str], ...] = (
    (
        "atlanta",
        "atlanta",
        "KATL",
        "USW00013874",
        33.6297,
        -84.4422,
        "America/New_York",
        "Atlanta (Hartsfield-Jackson)",
    ),
    (
        "austin",
        "austin",
        "KAUS",
        "USW00013904",
        30.1831,
        -97.6800,
        "America/Chicago",
        "Austin (Bergstrom)",
    ),
    (
        "chicago",
        "chicago",
        "KORD",
        "USW00094846",
        41.9603,
        -87.9317,
        "America/Chicago",
        "Chicago (O'Hare)",
    ),
    (
        "dallas",
        "dallas",
        "KDAL",
        "USW00013960",
        32.8383,
        -96.8358,
        "America/Chicago",
        "Dallas (Love Field)",
    ),
    (
        "denver",
        "denver",
        "KBKF",
        "USW00023036",
        39.7167,
        -104.7500,
        "America/Denver",
        "Denver area (Buckley Field)",
    ),
    (
        "houston",
        "houston",
        "KHOU",
        "USW00012918",
        29.6458,
        -95.2822,
        "America/Chicago",
        "Houston (Hobby)",
    ),
    (
        "los-angeles",
        "los-angeles",
        "KLAX",
        "USW00023174",
        33.9381,
        -118.3867,
        "America/Los_Angeles",
        "Los Angeles (LAX)",
    ),
    (
        "miami",
        "miami",
        "KMIA",
        "USW00012839",
        25.7881,
        -80.3169,
        "America/New_York",
        "Miami (International)",
    ),
    (
        "nyc",
        "nyc",
        "KLGA",
        "USW00014732",
        40.7794,
        -73.8803,
        "America/New_York",
        "New York City (LaGuardia)",
    ),
    (
        "san-francisco",
        "san-francisco",
        "KSFO",
        "USW00023234",
        37.6197,
        -122.3656,
        "America/Los_Angeles",
        "San Francisco (SFO)",
    ),
    (
        "seattle",
        "seattle",
        "KSEA",
        "USW00024233",
        47.4447,
        -122.3144,
        "America/Los_Angeles",
        "Seattle (Sea-Tac)",
    ),
)

# Additional METAR sites in the same metro (hourly_observations.site_icao).
# Primary ICAO on each Station is still the Polymarket resolution airport.
NEIGHBOR_ICAOS_BY_SLUG: Dict[str, Tuple[str, ...]] = {
    "nyc": ("KJFK", "KEWR"),
    "los-angeles": ("KBUR", "KVNY"),
    "san-francisco": ("KOAK", "KSJC"),
    "chicago": ("KMDW",),
    "dallas": ("KDFW",),
    "seattle": ("KBFI",),
    "miami": ("KFLL",),
    "houston": ("KIAH",),
    "denver": ("KCOS",),
    "atlanta": ("KPDK",),
    "austin": ("KGTU",),
}

REGISTRY: Dict[str, Station] = {
    row[0]: Station(
        slug=row[0],
        polymarket_city_slug=row[1],
        icao=row[2],
        ghcn_id=row[3],
        lat=row[4],
        lon=row[5],
        tz=row[6],
        display_name=row[7],
    )
    for row in _STATIONS
}

# Polymarket US daily-temperature city slugs (subset of global ``daily-temperature`` tag).
US_WEATHER_MARKET_SLUGS: Final[FrozenSet[str]] = frozenset(REGISTRY.keys())

_DEFAULT_STATION_LIST: Final[str] = ",".join(sorted(REGISTRY.keys()))


def default_station_csv() -> str:
    """Comma-separated slugs used when ``WEATHER_STATIONS`` is unset."""
    return _DEFAULT_STATION_LIST


def get(slug: str) -> Station:
    try:
        return REGISTRY[slug]
    except KeyError as exc:
        raise KeyError(
            f"Unknown station slug {slug!r}. Known: {sorted(REGISTRY)}"
        ) from exc


def all_stations() -> list[Station]:
    return list(REGISTRY.values())
