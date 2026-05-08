"""Live ASOS / METAR observations ingest for D+0 nowcasting.

Polymarket markets close 12:00 UTC on the target day. For East-Coast cities
that's 08:00 EDT — well before the typical afternoon high — but for any
station we can still narrow the predictive distribution by feeding the
*observed-so-far* high into the Phase 2 postprocessing model.

Sources written to ``observations`` (re-using the existing table):

* ``asos:live`` — latest hourly METAR temperatures from
  ``api.weather.gov/stations/{ICAO}/observations`` aggregated to a per-local-day
  max. Re-ingests overwrite the same ``(station_id, obs_date)`` row, with
  ``finalized=False`` until the WU resolution path catches up.
* ``mesonet:asos`` — Iowa Mesonet ASOS hourly archive, used as a backup when
  api.weather.gov is unavailable.

Note this is intentionally **not** the resolution source — Polymarket resolves
on Wunderground daily TMAX (already ingested by ``resolution_observations``).
``asos:live`` is purely for our own nowcasting feature.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Iterable
from zoneinfo import ZoneInfo

import httpx

from .. import config
from ..stations import REGISTRY, Station
from .observations import ObservationRow, persist_observations

log = logging.getLogger(__name__)

NWS_OBS_URL = "https://api.weather.gov/stations/{icao}/observations"
NWS_OBS_LATEST_URL = "https://api.weather.gov/stations/{icao}/observations/latest"

# Iowa Mesonet ASOS API. ``data=tmpf`` keeps the response small.
MESONET_URL = (
    "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
    "?station={icao}&data=tmpf&year1={y1}&month1={m1}&day1={d1}"
    "&year2={y2}&month2={m2}&day2={d2}&tz=Etc/UTC&format=onlycomma"
    "&latlon=no&missing=M&trace=T&direct=no&report_type=3"
)

ASOS_LIVE_SOURCE = "asos:live"
MESONET_SOURCE = "mesonet:asos"


@dataclass
class HourObs:
    valid_utc: dt.datetime
    temp_f: float


# ---------------------------------------------------------------------------
# api.weather.gov path
# ---------------------------------------------------------------------------


async def _fetch_nws_observations(
    client: httpx.AsyncClient,
    icao: str,
    *,
    headers: dict,
    hours_back: int = 36,
) -> list[HourObs]:
    """Pull recent observations from api.weather.gov."""
    params = {
        "start": (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_back)
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 200,
    }
    try:
        r = await client.get(
            NWS_OBS_URL.format(icao=icao),
            params=params,
            headers=headers,
            timeout=20.0,
        )
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.debug("NWS obs fetch failed for %s: %s", icao, exc)
        return []
    j = r.json()
    features = j.get("features") or []
    out: list[HourObs] = []
    for f in features:
        props = f.get("properties") or {}
        ts = props.get("timestamp")
        if not ts:
            continue
        temp_obj = props.get("temperature") or {}
        unit = (temp_obj.get("unitCode") or "").lower()
        v = temp_obj.get("value")
        if v is None:
            continue
        try:
            valid = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(
                dt.timezone.utc
            )
        except ValueError:
            continue
        # ``unit:degC`` is canonical; convert to F.
        if "degc" in unit or unit.endswith("celsius"):
            f_val = float(v) * 9.0 / 5.0 + 32.0
        elif "degf" in unit or unit.endswith("fahrenheit"):
            f_val = float(v)
        else:
            f_val = float(v) * 9.0 / 5.0 + 32.0
        if -80 <= f_val <= 130:
            out.append(HourObs(valid_utc=valid, temp_f=f_val))
    return out


# ---------------------------------------------------------------------------
# Iowa Mesonet path (CSV)
# ---------------------------------------------------------------------------


def _mesonet_window_url(icao: str, hours_back: int) -> str:
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(hours=hours_back)
    return MESONET_URL.format(
        icao=icao.lstrip("K"),  # Mesonet wants 3-letter for K-prefixed US stations
        y1=start.year, m1=start.month, d1=start.day,
        y2=end.year, m2=end.month, d2=end.day,
    )


async def _fetch_mesonet_observations(
    client: httpx.AsyncClient,
    icao: str,
    *,
    hours_back: int = 36,
) -> list[HourObs]:
    url = _mesonet_window_url(icao, hours_back)
    try:
        r = await client.get(url, timeout=20.0)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.debug("Mesonet ASOS fetch failed for %s: %s", icao, exc)
        return []
    text = r.text
    # CSV: ``station,valid,tmpf``
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    out: list[HourObs] = []
    for ln in lines[1:]:  # skip header
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
            out.append(HourObs(valid_utc=valid, temp_f=tmpf))
    return out


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _aggregate_daily_max(
    obs: list[HourObs], station: Station
) -> dict[dt.date, float]:
    tz = ZoneInfo(station.tz)
    by_day: dict[dt.date, float] = {}
    for o in obs:
        local_d = o.valid_utc.astimezone(tz).date()
        if local_d not in by_day or o.temp_f > by_day[local_d]:
            by_day[local_d] = o.temp_f
    return by_day


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def ingest_live_observations_async(
    station_slugs: Iterable[str],
    *,
    hours_back: int = 36,
) -> dict[str, int]:
    from ..db import station_id_by_slug

    sid_map = station_id_by_slug()
    headers = {"User-Agent": config.http_user_agent(), "Accept": "application/geo+json"}
    stations = [REGISTRY[s] for s in station_slugs if s in REGISTRY]

    total_rows = 0
    today = dt.date.today()

    async with httpx.AsyncClient(headers=headers) as client:
        for station in stations:
            sid = sid_map.get(station.slug)
            if sid is None:
                continue

            obs = await _fetch_nws_observations(
                client, station.icao, headers=headers, hours_back=hours_back
            )
            source = ASOS_LIVE_SOURCE
            if not obs:
                obs = await _fetch_mesonet_observations(
                    client, station.icao, hours_back=hours_back
                )
                source = MESONET_SOURCE
            if not obs:
                log.warning("No live obs available for %s", station.slug)
                continue

            daily = _aggregate_daily_max(obs, station)
            rows: list[ObservationRow] = []
            for d, mx in daily.items():
                rows.append(
                    ObservationRow(
                        station_id=sid,
                        obs_date=d,
                        source=source,
                        observed_max_f=int(round(mx)),
                        # Live data is never finalised; WU/NOAA paths set
                        # finalized=True after their respective windows.
                        finalized=False,
                    )
                )
            n = persist_observations(rows)
            total_rows += n
            log.info(
                "Persisted %d %s rows for %s (last day max=%.1fF)",
                n, source, station.slug, max(daily.values()) if daily else float("nan"),
            )

    return {"observations": total_rows}


def ingest_live_observations(
    station_slugs: Iterable[str],
    *,
    hours_back: int = 36,
) -> dict[str, int]:
    return asyncio.run(
        ingest_live_observations_async(
            station_slugs, hours_back=hours_back
        )
    )
