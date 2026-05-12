"""Historical hourly METAR ingest (Iowa Mesonet ASOS) into ``hourly_observations``.

Polymarket still resolves on **daily** WU TMAX at the named airport; this module
does **not** change resolution. It adds a higher-frequency observation archive so
we can:

* build **running max-so-far** features for short-lead models,
* blend **neighbor airport** temperatures for spatial context (urban heat island,
  sea breeze, frontal passages mis-aligned with a single METAR site).

Sources: Iowa Mesonet ``request/asos.py`` (same family as
:data:`polymarket_weather.data.live_observations.MESONET_URL`) with explicit
calendar date windows for backfill.

Neighbor ICAOs are listed in :data:`polymarket_weather.stations.NEIGHBOR_ICAOS_BY_SLUG`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from typing import Iterable

import httpx

from ..stations import NEIGHBOR_ICAOS_BY_SLUG, REGISTRY, Station
from ..db import with_conn

log = logging.getLogger(__name__)

SOURCE = "mesonet:asos_hourly"

# Same endpoint shape as live_observations.MESONET_URL but without tz=Etc/UTC
# in the middle — we keep UTC in the CSV ``valid`` column.
_MESONET_RANGE = (
    "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
    "?station={st}&data=tmpf&year1={y1}&month1={m1}&day1={d1}"
    "&year2={y2}&month2={m2}&day2={d2}&tz=Etc%2FUTC&format=onlycomma"
    "&latlon=no&missing=M&trace=T&direct=no&report_type=3"
)


def _mesonet_station_code(icao: str) -> str:
    """Mesonet expects US ids without leading K when 4-char K***."""
    icao = icao.strip().upper()
    if len(icao) == 4 and icao.startswith("K"):
        return icao[1:]
    return icao


def _mesonet_url(st: str, d0: dt.date, d1: dt.date) -> str:
    return _MESONET_RANGE.format(
        st=st,
        y1=d0.year, m1=d0.month, d1=d0.day,
        y2=d1.year, m2=d1.month, d2=d1.day,
    )


UPSERT_HOURLY_SQL = """
INSERT INTO hourly_observations
    (station_id, obs_ts_utc, site_icao, source, temp_f, raw)
VALUES (%s, %s, %s, %s, %s, %s::jsonb)
ON CONFLICT (station_id, obs_ts_utc, site_icao, source) DO UPDATE SET
    temp_f      = EXCLUDED.temp_f,
    raw         = EXCLUDED.raw,
    ingested_at = now()
"""


def _parse_mesonet_csv(text: str) -> list[tuple[dt.datetime, float]]:
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    out: list[tuple[dt.datetime, float]] = []
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < 3:
            continue
        valid_str, tmpf_str = parts[1].strip(), parts[2].strip()
        if not tmpf_str or tmpf_str in ("M", "T"):
            continue
        try:
            tmpf = float(tmpf_str)
            valid = dt.datetime.strptime(valid_str, "%Y-%m-%d %H:%M").replace(
                tzinfo=dt.timezone.utc
            )
        except ValueError:
            continue
        if -80 <= tmpf <= 130:
            out.append((valid, tmpf))
    return out


async def _fetch_range(
    client: httpx.AsyncClient,
    icao: str,
    d0: dt.date,
    d1: dt.date,
) -> list[tuple[dt.datetime, float]]:
    st = _mesonet_station_code(icao)
    url = _mesonet_url(st, d0, d1)
    try:
        r = await client.get(url, timeout=60.0)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("Mesonet hourly fetch failed %s %s..%s: %s", icao, d0, d1, exc)
        return []
    return _parse_mesonet_csv(r.text)


def _sites_for_station(station: Station) -> list[tuple[str, str]]:
    """(site_icao, role label for logging). Primary first, then neighbors."""
    primary = station.icao.upper()
    out = [(primary, "primary")]
    for nb in NEIGHBOR_ICAOS_BY_SLUG.get(station.slug, ()):
        nb = nb.upper()
        if nb != primary:
            out.append((nb, "neighbor"))
    return out


async def ingest_hourly_observations_async(
    station_slugs: Iterable[str],
    *,
    start_date: dt.date,
    end_date: dt.date,
    chunk_days: int = 120,
) -> dict[str, int]:
    """Fetch Mesonet ASOS hourly between ``start_date`` and ``end_date`` (inclusive).

    Long windows are split into ``chunk_days`` chunks to avoid Mesonet timeouts.
    """
    from ..db import station_id_by_slug

    if end_date < start_date:
        raise ValueError("end_date must be >= start_date")

    sid_map = station_id_by_slug()
    stations = [REGISTRY[s] for s in station_slugs if s in REGISTRY]
    total = 0

    async with httpx.AsyncClient() as client:
        for station in stations:
            sid = sid_map.get(station.slug)
            if sid is None:
                continue
            for site_icao, role in _sites_for_station(station):
                d0 = start_date
                while d0 <= end_date:
                    d1 = min(d0 + dt.timedelta(days=chunk_days - 1), end_date)
                    rows = await _fetch_range(client, site_icao, d0, d1)
                    if not rows:
                        log.debug("No hourly rows %s %s %s..%s", station.slug, site_icao, d0, d1)
                    batch = []
                    for ts, tf in rows:
                        batch.append(
                            (
                                sid,
                                ts,
                                site_icao.upper(),
                                SOURCE,
                                float(tf),
                                json.dumps({"role": role, "parent_slug": station.slug}),
                            )
                        )
                    if batch:
                        with with_conn() as conn, conn.cursor() as cur:
                            cur.executemany(UPSERT_HOURLY_SQL, batch)
                        total += len(batch)
                        log.info(
                            "Hourly obs: %s %s (%s) %s..%s → %d rows",
                            station.slug, site_icao, role, d0, d1, len(batch),
                        )
                    d0 = d1 + dt.timedelta(days=1)
                    await asyncio.sleep(0.35)
    return {"hourly_observations": total}


def ingest_hourly_observations(
    station_slugs: Iterable[str],
    *,
    start_date: dt.date,
    end_date: dt.date,
    chunk_days: int = 120,
) -> dict[str, int]:
    return asyncio.run(
        ingest_hourly_observations_async(
            station_slugs,
            start_date=start_date,
            end_date=end_date,
            chunk_days=chunk_days,
        )
    )
