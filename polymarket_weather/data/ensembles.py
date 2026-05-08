"""Ensemble forecast ingest.

Fetches per-member daily TMAX from Open-Meteo's ensemble endpoint
(`https://ensemble-api.open-meteo.com/v1/ensemble`), which exposes:

* ``gfs025``        — GFS Ensemble (~31 members)
* ``ecmwf_ifs04``   — ECMWF IFS Ensemble (~51 members)
* ``icon_seamless`` — ICON Ensemble
* ``gem_seamless``  — GEM Ensemble

For each member we compute the per-day max from the hourly trace and persist
into ``forecast_members``. We also write a "cheap CDF": the deterministic mean
across members goes into the regular ``forecasts`` table as
``openmeteo_ens:<model>`` so the existing M0/M1 + EMOS code paths can read a
single per-source mean without joining to ``forecast_members``.

We *deliberately* don't pull AIFS-ENS / GEFS GRIB2 from AWS Open Data here
because that requires ``cfgrib``/``xarray`` which is heavyweight for tiny
bankroll. The Open-Meteo ensemble endpoint exposes the same models via a
clean JSON API and is sufficient for our edge analysis.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import statistics
from dataclasses import dataclass
from typing import Iterable
from zoneinfo import ZoneInfo

import httpx

from .. import config
from ..stations import REGISTRY, Station
from .forecasts import (
    ForecastRow,
    _get_with_retry,
    persist_forecasts,
)

log = logging.getLogger(__name__)

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

# (open-meteo model name, our internal source label, expected member count).
ENSEMBLE_MODELS: list[tuple[str, str, int]] = [
    ("gfs025", "openmeteo_ens:gfs025", 31),
    ("ecmwf_ifs04", "openmeteo_ens:ecmwf_ifs04", 51),
    ("icon_seamless", "openmeteo_ens:icon_seamless", 40),
    ("gem_seamless", "openmeteo_ens:gem_seamless", 21),
]


@dataclass
class MemberRow:
    station_id: int
    target_date: dt.date
    source: str
    run_time: dt.datetime
    member_id: int
    predicted_max_f: float
    raw: dict


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------


async def _fetch_one_model(
    client: httpx.AsyncClient,
    station: Station,
    *,
    model: str,
    source_label: str,
    past_days: int,
    forecast_days: int,
) -> tuple[list[MemberRow], list[ForecastRow]]:
    """Pull hourly traces, compute per-member daily max, return both
    per-member rows and an aggregate "ensemble mean" deterministic row."""
    params = {
        "latitude": station.lat,
        "longitude": station.lon,
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "timezone": station.tz,
        "models": model,
        "past_days": past_days,
        "forecast_days": forecast_days,
    }
    try:
        r = await _get_with_retry(client, ENSEMBLE_URL, params=params)
    except httpx.HTTPError as exc:
        log.warning("Ensemble fetch failed for %s/%s: %s", station.slug, model, exc)
        return [], []

    j = r.json()
    hourly = j.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return [], []

    tz = ZoneInfo(station.tz)
    today_local = dt.datetime.now(tz).date()
    horizon = today_local + dt.timedelta(days=forecast_days)
    past_floor = today_local - dt.timedelta(days=past_days)

    run_time = dt.datetime.now(dt.timezone.utc)
    cw = j.get("current_weather") or {}
    if isinstance(cw, dict) and cw.get("time"):
        try:
            run_time = (
                dt.datetime.fromisoformat(cw["time"])
                .replace(tzinfo=ZoneInfo(station.tz))
                .astimezone(dt.timezone.utc)
            )
        except Exception:
            pass

    # Find every member key (``temperature_2m``, ``temperature_2m_member01``, ...).
    member_keys: list[tuple[int, str]] = []
    for k in hourly:
        if k == "temperature_2m":
            member_keys.append((0, k))
        elif k.startswith("temperature_2m_member"):
            try:
                mid = int(k.replace("temperature_2m_member", ""))
            except ValueError:
                continue
            member_keys.append((mid, k))
    member_keys.sort()
    if not member_keys:
        return [], []

    # Parse times as local naive ISO strings; bucket each member by date.
    parsed_times: list[dt.date | None] = []
    for ts in times:
        try:
            parsed_times.append(dt.date.fromisoformat(ts.split("T")[0]))
        except (ValueError, AttributeError):
            parsed_times.append(None)

    member_rows: list[MemberRow] = []
    # Per-day per-member max. Then aggregate to ensemble mean per day.
    by_day_member: dict[tuple[dt.date, int], float] = {}
    for mid, key in member_keys:
        values = hourly.get(key) or []
        for d, v in zip(parsed_times, values):
            if d is None or v is None:
                continue
            if d < past_floor or d > horizon:
                continue
            key2 = (d, mid)
            if key2 not in by_day_member or float(v) > by_day_member[key2]:
                by_day_member[key2] = float(v)

    by_day_values: dict[dt.date, list[float]] = {}
    for (d, mid), v in by_day_member.items():
        by_day_values.setdefault(d, []).append(v)
        member_rows.append(
            MemberRow(
                station_id=0,
                target_date=d,
                source=source_label,
                run_time=run_time,
                member_id=mid,
                predicted_max_f=v,
                raw={"model": model, "member": mid},
            )
        )

    aggregate: list[ForecastRow] = []
    for d, vals in by_day_values.items():
        if not vals:
            continue
        mean = statistics.fmean(vals)
        std = statistics.pstdev(vals) if len(vals) > 1 else None
        aggregate.append(
            ForecastRow(
                station_id=0,
                target_date=d,
                source=source_label,
                run_time=run_time,
                predicted_max_f=mean,
                predicted_std_f=std,
                raw={
                    "model": model,
                    "n_members": len(vals),
                    "min": min(vals),
                    "max": max(vals),
                    "p10": _percentile(vals, 10),
                    "p50": _percentile(vals, 50),
                    "p90": _percentile(vals, 90),
                },
            )
        )
    return member_rows, aggregate


def _percentile(vals: list[float], p: float) -> float:
    if not vals:
        return float("nan")
    s = sorted(vals)
    idx = (p / 100.0) * (len(s) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] * (1 - frac) + s[hi] * frac


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


UPSERT_MEMBER_SQL = """
INSERT INTO forecast_members
    (station_id, target_date, source, run_time, member_id,
     predicted_max_f, raw, ingested_at)
VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, now())
ON CONFLICT (station_id, target_date, source, run_time, member_id) DO UPDATE SET
    predicted_max_f = EXCLUDED.predicted_max_f,
    raw             = EXCLUDED.raw,
    ingested_at     = now()
"""


def persist_members(rows: list[MemberRow]) -> int:
    if not rows:
        return 0
    from ..db import with_conn

    n = 0
    with with_conn() as conn, conn.cursor() as cur:
        for r in rows:
            cur.execute(
                UPSERT_MEMBER_SQL,
                (
                    r.station_id,
                    r.target_date,
                    r.source,
                    r.run_time,
                    r.member_id,
                    r.predicted_max_f,
                    json.dumps(r.raw),
                ),
            )
            n += 1
    return n


async def ingest_ensembles_async(
    station_slugs: Iterable[str],
    *,
    past_days: int = 7,
    forecast_days: int = 8,
) -> dict[str, int]:
    from ..db import station_id_by_slug

    sid_map = station_id_by_slug()
    headers = {"User-Agent": config.http_user_agent(), "Accept": "application/json"}
    stations = [REGISTRY[s] for s in station_slugs if s in REGISTRY]

    member_rows: list[MemberRow] = []
    agg_rows: list[ForecastRow] = []

    sem = asyncio.Semaphore(2)

    async def _runner(s: Station) -> None:
        sid = sid_map.get(s.slug)
        if sid is None:
            return
        async with sem:
            async with httpx.AsyncClient(headers=headers) as client:
                for model, source_label, _expected in ENSEMBLE_MODELS:
                    try:
                        members, agg = await _fetch_one_model(
                            client,
                            s,
                            model=model,
                            source_label=source_label,
                            past_days=past_days,
                            forecast_days=forecast_days,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "Ensemble model %s failed for %s: %s",
                            model, s.slug, exc,
                        )
                        continue
                    for mr in members:
                        mr.station_id = sid
                    for ar in agg:
                        ar.station_id = sid
                    member_rows.extend(members)
                    agg_rows.extend(agg)

    await asyncio.gather(*(_runner(s) for s in stations))

    n_members = persist_members(member_rows)
    n_agg = persist_forecasts(agg_rows)
    log.info(
        "Persisted %d ensemble member rows + %d aggregate forecast rows.",
        n_members, n_agg,
    )
    return {"forecast_members": n_members, "forecasts": n_agg}


def ingest_ensembles(
    station_slugs: Iterable[str],
    *,
    past_days: int = 7,
    forecast_days: int = 8,
) -> dict[str, int]:
    return asyncio.run(
        ingest_ensembles_async(
            station_slugs,
            past_days=past_days,
            forecast_days=forecast_days,
        )
    )
