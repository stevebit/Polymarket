"""Same-day RAP nowcast feature.

Review §4.5: for the lead-0 component of M2 we want the **most recent**
information possible — the temperature recorded so far at the resolution
airport (max-so-far from ``hourly_observations``) plus the **expected
remaining uplift** from a high-resolution nowcast model.

NOAA RAP (Rapid Refresh) is the natural choice: 13 km, hourly cycles,
0–18 h horizon, and Open-Meteo exposes it under ``models=rap_conus`` on
the GFS endpoint. We fetch the next 6 hours of ``temperature_2m`` and
subtract the running max-so-far to derive ``today_remaining_uplift_f``.

This module is **read-only**: it does not persist anything. The caller
(M2's ``_build_components`` for lead 0) consumes the scalar feature
directly so we don't grow the schema for a transient signal.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import httpx

from .. import config
from ..stations import REGISTRY, Station
from .forecasts import _get_with_retry, model_cycle_at

log = logging.getLogger(__name__)

RAP_ENDPOINT = "https://api.open-meteo.com/v1/gfs"
RAP_MODEL = "rap_conus"
SOURCE = "openmeteo:rap_conus"

# Preliminary uncertainty (review §4.5): RMSE of RAP next-6h vs ASOS hourly
# temperature is ~1.5°F in our pilot stations. This will be refit on
# Phase 4 backfill data when available.
DEFAULT_RAP_RMSE_F = 1.5


@dataclass(frozen=True)
class NowcastFeature:
    """Same-day uplift signal for ``M2`` lead-0 component."""

    station_slug: str
    target_date: dt.date
    as_of: dt.datetime
    max_so_far_f: float | None
    expected_uplift_f: float | None
    expected_max_f: float | None
    rap_rmse_f: float = DEFAULT_RAP_RMSE_F


async def _fetch_rap_trace(
    client: httpx.AsyncClient, station: Station, *, hours: int = 6,
) -> list[tuple[dt.datetime, float]]:
    """Return ``[(ts_local, temp_f), ...]`` for the next ``hours`` hours."""
    params = {
        "latitude": station.lat,
        "longitude": station.lon,
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "timezone": station.tz,
        "models": RAP_MODEL,
        "past_days": 0,
        "forecast_days": 1,
    }
    r = await _get_with_retry(client, RAP_ENDPOINT, params=params)
    j = r.json()
    block = j.get("hourly") or {}
    times = block.get("time") or []
    temps = block.get("temperature_2m") or []
    tz = ZoneInfo(station.tz)
    now_local = dt.datetime.now(tz)
    out: list[tuple[dt.datetime, float]] = []
    for ts_str, v in zip(times, temps):
        if v is None:
            continue
        try:
            ts = dt.datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=tz)
        if ts < now_local:
            continue
        out.append((ts, float(v)))
        if len(out) >= hours:
            break
    return out


def _max_so_far(cur, station_id: int, target_date: dt.date) -> float | None:
    """Maximum ``temp_f`` observed today on the resolution airport.

    Reads ``hourly_observations`` (already ingested by
    ``ingest_hourly_observations``). Returns ``None`` if no rows exist yet.
    """
    cur.execute(
        """
        SELECT MAX(temp_f)::float8
        FROM hourly_observations
        WHERE station_id = %s
          AND DATE(obs_ts_utc AT TIME ZONE 'UTC') = %s
        """,
        (station_id, target_date),
    )
    row = cur.fetchone()
    return None if row is None or row[0] is None else float(row[0])


async def fetch_nowcast(
    station_slug: str, target_date: dt.date, *, as_of: dt.datetime | None = None,
) -> NowcastFeature | None:
    """Build a same-day uplift feature for one station.

    Returns ``None`` when there's nothing useful (no RAP data, no
    max-so-far). The caller fall back to the deterministic M2 components.
    """
    station = REGISTRY.get(station_slug)
    if station is None:
        return None

    now = as_of or dt.datetime.now(dt.timezone.utc)
    headers = {"User-Agent": config.http_user_agent(), "Accept": "application/json"}

    rap_trace: list[tuple[dt.datetime, float]] = []
    try:
        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            rap_trace = await _fetch_rap_trace(client, station, hours=6)
    except Exception as exc:  # noqa: BLE001
        log.info("RAP nowcast failed for %s: %s", station_slug, exc)

    from ..db import station_id_by_slug, with_conn

    sid = station_id_by_slug().get(station_slug)
    if sid is None:
        return None

    with with_conn() as conn, conn.cursor() as cur:
        max_so_far = _max_so_far(cur, sid, target_date)

    rap_peak = max((t for _, t in rap_trace), default=None)
    expected_uplift = None
    expected_max = None
    if rap_peak is not None and max_so_far is not None:
        expected_uplift = max(0.0, rap_peak - max_so_far)
        expected_max = max_so_far + expected_uplift
    elif rap_peak is not None:
        expected_max = rap_peak
    elif max_so_far is not None:
        # Without a nowcast we still expose what we know.
        expected_max = max_so_far

    if max_so_far is None and rap_peak is None:
        return None

    return NowcastFeature(
        station_slug=station_slug,
        target_date=target_date,
        as_of=now,
        max_so_far_f=max_so_far,
        expected_uplift_f=expected_uplift,
        expected_max_f=expected_max,
        rap_rmse_f=DEFAULT_RAP_RMSE_F,
    )
