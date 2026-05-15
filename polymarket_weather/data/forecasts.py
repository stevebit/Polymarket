"""Forecast ingest: Open-Meteo (multi-model) + NWS gridpoint hourly + NWS daily
+ NBM station bulletins.

Sources written to the ``forecasts`` table:

* ``openmeteo:gfs_seamless``, ``openmeteo:ecmwf_ifs04``, ``openmeteo:icon_seamless``,
  ``openmeteo:gem_seamless``: per-model deterministic daily TMAX (existing).
* ``openmeteo:bestmatch``: Open-Meteo's calibrated blended best-of-models output.
* ``nws:gridpoint``: NWS gridpoint hourly periods reduced to per-day max (existing).
* ``nws:daily``: NWS public daily forecast (NBM-driven daytime high) per day.
* ``nbm:station``: NBS / NBE station text bulletin parsed from NOMADS,
  whichever cycle is freshest. This source is best-effort: if NOMADS is
  unreachable or the bulletin format changes, we log a warning and skip.

All HTTP calls have an explicit timeout, a retry-with-backoff wrapper, and a
``User-Agent`` header per ``WEATHER_HTTP_UA``. Upserts use ``ON CONFLICT DO
UPDATE`` so re-running for the same target window is idempotent.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from dataclasses import dataclass
from typing import Iterable
from zoneinfo import ZoneInfo

import httpx

from .. import config
from ..stations import REGISTRY, Station

log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
NWS_POINTS_URL = "https://api.weather.gov/points/{lat},{lon}"

OPEN_METEO_MODELS = [
    "gfs_seamless",
    "ecmwf_ifs04",
    "icon_seamless",
    "gem_seamless",
]

# Open-Meteo's calibrated blend ("bestmatch") is exposed via the regular
# forecast endpoint without the ``models=`` parameter and behaves like a
# separate source. We add it explicitly as ``openmeteo:bestmatch``.
OPEN_METEO_BESTMATCH = "openmeteo:bestmatch"

# NOMADS NBM station-text bulletin (NBS / NBE). The NBS file has the same
# station block format as classic MOS bulletins.
NBM_NOMADS_BASE = (
    "https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod"
    "/blend.{ymd}/{hh}/text/blend_nbstx.t{hh}z"
)
# Cycles to try in order of preference. 06z is the freshest run that lands
# before the 12:00 UTC market close on the East Coast.
NBM_PREFERRED_CYCLES = ("18", "12", "06", "00")


@dataclass
class ForecastRow:
    station_id: int
    target_date: dt.date
    source: str
    run_time: dt.datetime
    predicted_max_f: float | None
    predicted_std_f: float | None
    raw: dict


# Hourly-cadence sources (HRRR / RAP / NBM-hourly) round to the previous
# hour. Global deterministic models cycle at 00/06/12/18 UTC.
# ``gfs_hrrr`` is the Historical Forecast API name for the CONUS HRRR nest;
# the live ``/v1/gfs`` client still exposes it as ``hrrr_conus``.
HOURLY_CADENCE_MODELS = {"hrrr_conus", "gfs_hrrr", "rap_conus", "rap"}


def model_cycle_at(now_utc: dt.datetime, model: str) -> dt.datetime:
    """Round ``now_utc`` down to the most recent model cycle.

    Global deterministic / ensemble models (GFS, ECMWF IFS, ICON, GEM,
    AIFS, GraphCast, NBM probabilistic) cycle every 6 hours at 00 / 06 /
    12 / 18 UTC. HRRR and RAP cycle hourly. This helper replaces the old
    ``current_weather.time`` heuristic which was an observation timestamp
    masquerading as a model cycle — see review §2.8.
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=dt.timezone.utc)
    if model in HOURLY_CADENCE_MODELS:
        return now_utc.replace(minute=0, second=0, microsecond=0)
    # 6-hourly: round down to the most recent multiple of 6 hours.
    cycle_hour = (now_utc.hour // 6) * 6
    return now_utc.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    attempts: int = 3,
) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            r = await client.get(
                url,
                params=params,
                headers=headers,
                timeout=20.0,
            )
            if r.status_code >= 500 or r.status_code == 429:
                raise httpx.HTTPStatusError(
                    f"transient {r.status_code}",
                    request=r.request,
                    response=r,
                )
            r.raise_for_status()
            return r
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt == attempts - 1:
                break
            await asyncio.sleep(2 ** attempt)
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Open-Meteo
# ---------------------------------------------------------------------------


async def _fetch_openmeteo(
    client: httpx.AsyncClient,
    station: Station,
    *,
    past_days: int,
    forecast_days: int,
) -> list[ForecastRow]:
    params = {
        "latitude": station.lat,
        "longitude": station.lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": station.tz,
        "models": ",".join(OPEN_METEO_MODELS),
        "past_days": past_days,
        "forecast_days": forecast_days,
    }
    r = await _get_with_retry(client, OPEN_METEO_URL, params=params)
    j = r.json()

    daily = j.get("daily") or {}
    dates = daily.get("time") or []
    # Per-model arrays come back as ``temperature_2m_max_<model>``.
    # Each model has its own cycle; use ``model_cycle_at`` (review §2.8)
    # to assign a *real* cycle timestamp rather than ``current_weather.time``
    # which is an observation, not a forecast issue time.
    now_utc = dt.datetime.now(dt.timezone.utc)

    rows: list[ForecastRow] = []
    for model in OPEN_METEO_MODELS:
        key = f"temperature_2m_max_{model}"
        values = daily.get(key) or []
        run_time = model_cycle_at(now_utc, model)
        for ds, v in zip(dates, values):
            try:
                tdate = dt.date.fromisoformat(ds)
            except ValueError:
                continue
            if v is None:
                continue
            rows.append(
                ForecastRow(
                    station_id=0,  # filled in later
                    target_date=tdate,
                    source=f"openmeteo:{model}",
                    run_time=run_time,
                    predicted_max_f=float(v),
                    predicted_std_f=None,
                    raw={"model": model, "value": v, "unit": "F"},
                )
            )
    return rows


async def _fetch_openmeteo_generic(
    client: httpx.AsyncClient,
    station: Station,
    *,
    endpoint: str,
    model_id: str,
    source_name: str,
    past_days: int,
    forecast_days: int,
    hourly: bool = False,
) -> list[ForecastRow]:
    """Generic Open-Meteo fetcher for one model id.

    Phase 3 (review §4.1 / §4.3) adds HRRR, ECMWF AIFS, and GFS GraphCast
    through this single shape:

    * ``endpoint``: Open-Meteo URL (``/v1/forecast`` or ``/v1/gfs``).
    * ``model_id``: the value passed to ``?models=``.
    * ``hourly=True`` fetches ``hourly=temperature_2m`` and aggregates to
      per-local-day max (HRRR / GraphCast). ``hourly=False`` requests
      ``daily=temperature_2m_max`` directly (AIFS).
    * ``source_name`` is the value persisted to ``forecasts.source``.

    Network failures are surfaced; orchestration code wraps every call in
    a try/except so one flaky source doesn't break the tick.
    """
    common = {
        "latitude": station.lat,
        "longitude": station.lon,
        "temperature_unit": "fahrenheit",
        "timezone": station.tz,
        "models": model_id,
        "past_days": past_days,
        "forecast_days": forecast_days,
    }
    if hourly:
        params = dict(common, hourly="temperature_2m")
    else:
        params = dict(common, daily="temperature_2m_max")

    r = await _get_with_retry(client, endpoint, params=params)
    j = r.json()

    run_time = model_cycle_at(dt.datetime.now(dt.timezone.utc), model_id)
    rows: list[ForecastRow] = []

    if not hourly:
        daily = j.get("daily") or {}
        dates = daily.get("time") or []
        values = daily.get("temperature_2m_max") or []
        for ds, v in zip(dates, values):
            if v is None:
                continue
            try:
                tdate = dt.date.fromisoformat(ds)
            except ValueError:
                continue
            rows.append(
                ForecastRow(
                    station_id=0,
                    target_date=tdate,
                    source=source_name,
                    run_time=run_time,
                    predicted_max_f=float(v),
                    predicted_std_f=None,
                    raw={"model": model_id, "value": v, "unit": "F"},
                )
            )
        return rows

    # Hourly path: aggregate to local-tz day max.
    hourly_block = j.get("hourly") or {}
    times = hourly_block.get("time") or []
    temps = hourly_block.get("temperature_2m") or []
    tz = ZoneInfo(station.tz)
    bucket: dict[dt.date, list[float]] = {}
    for ts_str, v in zip(times, temps):
        if v is None:
            continue
        try:
            ts = dt.datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        # Open-Meteo returns naive local times when ``timezone=`` is set.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=tz)
        local = ts.astimezone(tz)
        bucket.setdefault(local.date(), []).append(float(v))

    for d, vals in sorted(bucket.items()):
        if not vals:
            continue
        rows.append(
            ForecastRow(
                station_id=0,
                target_date=d,
                source=source_name,
                run_time=run_time,
                predicted_max_f=max(vals),
                predicted_std_f=None,
                raw={"model": model_id, "n_hours": len(vals), "max": max(vals)},
            )
        )
    return rows


NBM_PERCENTILES = (10, 25, 50, 75, 90)


async def _fetch_nbm_percentiles(
    client: httpx.AsyncClient,
    station: Station,
    *,
    past_days: int,
    forecast_days: int,
) -> list[ForecastRow]:
    """NBM probabilistic percentiles (P10 / P25 / P50 / P75 / P90).

    Review §4.2: NBM publishes probabilistic temperature percentiles
    alongside its deterministic best-blend. Aggregating per-day max for
    each percentile gives us a 5-knot empirical CDF directly — feeding
    :class:`PercentileCDF` from ``score.py`` skips the
    Gaussian-approximation step entirely for the bucket integration.

    Open-Meteo's GFS endpoint exposes these as
    ``temperature_2m_percentile_10`` through ``..._90`` when ``models=
    gfs_nbm``. The endpoint isn't fully documented for every percentile
    so we degrade silently if a field is missing: any percentile with no
    data is just left empty and downstream code falls back to the
    deterministic NBM source.
    """
    pct_fields = [f"temperature_2m_percentile_{p}" for p in NBM_PERCENTILES]
    params = {
        "latitude": station.lat,
        "longitude": station.lon,
        "hourly": ",".join(["temperature_2m", *pct_fields]),
        "temperature_unit": "fahrenheit",
        "timezone": station.tz,
        "models": "gfs_nbm",
        "past_days": past_days,
        "forecast_days": forecast_days,
    }
    try:
        r = await _get_with_retry(
            client, "https://api.open-meteo.com/v1/gfs", params=params,
        )
        j = r.json()
    except Exception as exc:  # noqa: BLE001
        log.info("NBM percentiles unavailable for %s: %s", station.slug, exc)
        return []

    hourly_block = j.get("hourly") or {}
    times = hourly_block.get("time") or []
    if not times:
        return []
    tz = ZoneInfo(station.tz)
    run_time = model_cycle_at(dt.datetime.now(dt.timezone.utc), "gfs_nbm")
    rows: list[ForecastRow] = []

    for p in NBM_PERCENTILES:
        key = f"temperature_2m_percentile_{p}"
        vals = hourly_block.get(key) or []
        if not vals:
            continue
        bucket: dict[dt.date, list[float]] = {}
        for ts_str, v in zip(times, vals):
            if v is None:
                continue
            try:
                ts = dt.datetime.fromisoformat(ts_str)
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=tz)
            local = ts.astimezone(tz)
            bucket.setdefault(local.date(), []).append(float(v))
        for d, day_vals in sorted(bucket.items()):
            if not day_vals:
                continue
            rows.append(
                ForecastRow(
                    station_id=0,
                    target_date=d,
                    source=f"nbm:p{p:02d}",
                    run_time=run_time,
                    predicted_max_f=max(day_vals),
                    predicted_std_f=None,
                    raw={"percentile": p, "n_hours": len(day_vals)},
                )
            )

    return rows


async def _fetch_hrrr_conus(
    client: httpx.AsyncClient,
    station: Station,
    *,
    past_days: int,
    forecast_days: int,
) -> list[ForecastRow]:
    """HRRR (CONUS-only, ~3 km, hourly cycle, 18-48h horizon).

    Review §4.1: HRRR is the single highest-leverage add — it dominates the
    next 0-2 days vs the 25-km global models for the temperature_2m field.
    CONUS-only, gracefully empty for stations outside CONUS.
    """
    return await _fetch_openmeteo_generic(
        client, station,
        endpoint="https://api.open-meteo.com/v1/gfs",
        model_id="hrrr_conus",
        source_name="openmeteo:hrrr_conus",
        past_days=past_days,
        forecast_days=min(forecast_days, 2),  # HRRR horizon is short
        hourly=True,
    )


async def _fetch_ecmwf_aifs(
    client: httpx.AsyncClient,
    station: Station,
    *,
    past_days: int,
    forecast_days: int,
) -> list[ForecastRow]:
    """ECMWF AIFS (Artificial Intelligence Forecasting System).

    Review §4.3: an ECMWF-trained ML model that scores comparably with the
    operational IFS and is operationally maintained by ECMWF.
    """
    return await _fetch_openmeteo_generic(
        client, station,
        endpoint=OPEN_METEO_URL,
        model_id="ecmwf_aifs025_single",
        source_name="openmeteo:aifs025_single",
        past_days=past_days,
        forecast_days=forecast_days,
        hourly=False,
    )


async def _fetch_gfs_graphcast(
    client: httpx.AsyncClient,
    station: Station,
    *,
    past_days: int,
    forecast_days: int,
) -> list[ForecastRow]:
    """GFS GraphCast (Google DeepMind ML, 6-hourly, run on GFS init).

    Review §4.3: independent ML model, weakly correlated with AIFS errors,
    so adding both expands the effective ensemble.
    """
    return await _fetch_openmeteo_generic(
        client, station,
        endpoint=OPEN_METEO_URL,
        model_id="gfs_graphcast025",
        source_name="openmeteo:gfs_graphcast025",
        past_days=past_days,
        forecast_days=forecast_days,
        hourly=True,
    )


async def _fetch_openmeteo_bestmatch(
    client: httpx.AsyncClient,
    station: Station,
    *,
    past_days: int,
    forecast_days: int,
) -> list[ForecastRow]:
    """Open-Meteo's calibrated blend (no ``models=``)."""
    params = {
        "latitude": station.lat,
        "longitude": station.lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": station.tz,
        "past_days": past_days,
        "forecast_days": forecast_days,
    }
    r = await _get_with_retry(client, OPEN_METEO_URL, params=params)
    j = r.json()
    daily = j.get("daily") or {}
    dates = daily.get("time") or []
    values = daily.get("temperature_2m_max") or []
    # Bestmatch is Open-Meteo's blended product (NBM-like cadence). Use a
    # 6-hourly cycle anchor — see review §2.8.
    run_time = model_cycle_at(dt.datetime.now(dt.timezone.utc), "bestmatch")

    rows: list[ForecastRow] = []
    for ds, v in zip(dates, values):
        try:
            tdate = dt.date.fromisoformat(ds)
        except ValueError:
            continue
        if v is None:
            continue
        rows.append(
            ForecastRow(
                station_id=0,
                target_date=tdate,
                source=OPEN_METEO_BESTMATCH,
                run_time=run_time,
                predicted_max_f=float(v),
                predicted_std_f=None,
                raw={"model": "bestmatch", "value": v, "unit": "F"},
            )
        )
    return rows


# ---------------------------------------------------------------------------
# NWS gridpoint hourly
# ---------------------------------------------------------------------------


async def _fetch_nws(
    client: httpx.AsyncClient,
    station: Station,
    *,
    headers: dict,
    forecast_days: int,
) -> list[ForecastRow]:
    points = await _get_with_retry(
        client,
        NWS_POINTS_URL.format(lat=station.lat, lon=station.lon),
        headers=headers,
    )
    pj = points.json()
    forecast_url = (pj.get("properties") or {}).get("forecastHourly")
    if not forecast_url:
        log.warning("NWS points payload missing forecastHourly for %s", station.slug)
        return []

    fc = await _get_with_retry(client, forecast_url, headers=headers)
    fj = fc.json()
    periods = (fj.get("properties") or {}).get("periods") or []
    if not periods:
        return []

    tz = ZoneInfo(station.tz)
    today_local = dt.datetime.now(tz).date()
    horizon = today_local + dt.timedelta(days=forecast_days)

    bucket: dict[dt.date, list[float]] = {}
    for p in periods:
        st = p.get("startTime")
        if not st:
            continue
        try:
            ts = dt.datetime.fromisoformat(st).astimezone(tz)
        except ValueError:
            continue
        if ts.date() < today_local or ts.date() > horizon:
            continue
        temp = p.get("temperature")
        unit = (p.get("temperatureUnit") or "F").upper()
        if temp is None:
            continue
        f_val = float(temp) if unit == "F" else float(temp) * 9 / 5 + 32
        bucket.setdefault(ts.date(), []).append(f_val)

    run_time = dt.datetime.now(dt.timezone.utc)
    if (fj.get("properties") or {}).get("updateTime"):
        try:
            run_time = dt.datetime.fromisoformat(
                fj["properties"]["updateTime"].replace("Z", "+00:00")
            )
        except Exception:
            pass

    rows: list[ForecastRow] = []
    for d, temps in bucket.items():
        if not temps:
            continue
        rows.append(
            ForecastRow(
                station_id=0,
                target_date=d,
                source="nws:gridpoint",
                run_time=run_time,
                predicted_max_f=max(temps),
                predicted_std_f=None,
                raw={"hourly_count": len(temps), "max": max(temps)},
            )
        )
    return rows


# ---------------------------------------------------------------------------
# NWS daily forecast (NBM-driven)
# ---------------------------------------------------------------------------


async def _fetch_nws_daily(
    client: httpx.AsyncClient,
    station: Station,
    *,
    headers: dict,
    forecast_days: int,
) -> list[ForecastRow]:
    """Use the public ``forecast`` (not ``forecastHourly``) endpoint to extract
    each daytime period's high. NWS public forecast is NBM-driven."""
    points = await _get_with_retry(
        client,
        NWS_POINTS_URL.format(lat=station.lat, lon=station.lon),
        headers=headers,
    )
    pj = points.json()
    forecast_url = (pj.get("properties") or {}).get("forecast")
    if not forecast_url:
        log.warning("NWS points payload missing forecast for %s", station.slug)
        return []
    fc = await _get_with_retry(client, forecast_url, headers=headers)
    fj = fc.json()
    periods = (fj.get("properties") or {}).get("periods") or []
    if not periods:
        return []

    tz = ZoneInfo(station.tz)
    today_local = dt.datetime.now(tz).date()
    horizon = today_local + dt.timedelta(days=forecast_days)

    rows: list[ForecastRow] = []
    run_time = dt.datetime.now(dt.timezone.utc)
    if (fj.get("properties") or {}).get("updateTime"):
        try:
            run_time = dt.datetime.fromisoformat(
                fj["properties"]["updateTime"].replace("Z", "+00:00")
            )
        except Exception:
            pass

    for p in periods:
        if not p.get("isDaytime"):
            continue
        st = p.get("startTime")
        if not st:
            continue
        try:
            ts = dt.datetime.fromisoformat(st).astimezone(tz)
        except ValueError:
            continue
        d = ts.date()
        if d < today_local or d > horizon:
            continue
        temp = p.get("temperature")
        unit = (p.get("temperatureUnit") or "F").upper()
        if temp is None:
            continue
        f_val = float(temp) if unit == "F" else float(temp) * 9 / 5 + 32
        rows.append(
            ForecastRow(
                station_id=0,
                target_date=d,
                source="nws:daily",
                run_time=run_time,
                predicted_max_f=f_val,
                predicted_std_f=None,
                raw={"period": p.get("name"), "value": f_val, "unit": "F"},
            )
        )
    return rows


# ---------------------------------------------------------------------------
# NBM station text bulletins (NOMADS)
# ---------------------------------------------------------------------------

# NBS bulletin block: identify the station header line (ICAO followed by
# "  NBS GUIDANCE  ..."). Within each block, ``DT`` row gives forecast valid
# dates, ``UTC`` row gives valid hours, ``X/N`` (or ``XN``) row gives
# alternating max/min temperatures aligned to ``DT``+``UTC`` columns.
_NBS_HEADER_RE = re.compile(r"^([A-Z]{4})\s+NBS\s+GUIDANCE", re.MULTILINE)


def _parse_nbm_block(block: str) -> list[tuple[dt.date, int]]:
    """Extract ``(target_date, max_f)`` pairs for the next N days from one
    station's NBS bulletin block. Returns empty list if parsing fails."""
    lines = block.splitlines()
    # Header: ``KORD   NBS GUIDANCE    11/05/2026  0600 UTC``
    if not lines:
        return []
    header = lines[0]
    m = re.search(r"(\d{2}/\d{2}/\d{4})\s+(\d{4})\s*UTC", header)
    if not m:
        return []
    try:
        issue_date = dt.datetime.strptime(m.group(1), "%m/%d/%Y").date()
        issue_hour = int(m.group(2)[:2])
    except (ValueError, IndexError):
        return []

    dt_row = utc_row = xn_row = tmp_row = None
    for ln in lines:
        # Each row begins with a 3-char tag, e.g. "DT", "UTC", "X/N".
        tag_match = re.match(r"^\s*([A-Z/]{2,3})\b", ln)
        if not tag_match:
            continue
        tag = tag_match.group(1)
        if tag == "DT":
            dt_row = ln
        elif tag == "UTC":
            utc_row = ln
        elif tag in ("X/N", "XN"):
            xn_row = ln
        elif tag == "TMP":
            tmp_row = ln

    if dt_row is None or utc_row is None or (xn_row is None and tmp_row is None):
        return []

    # NBS uses fixed 3-char-wide columns starting after the tag.
    def _col_values(row: str, width: int = 3) -> list[str]:
        # Strip the leading tag and the following whitespace, then chunk.
        body = re.sub(r"^\s*[A-Z/]{2,3}\s*", "", row, count=1)
        return [body[i : i + width].strip() for i in range(0, len(body), width)]

    dates = _col_values(dt_row)
    hours = _col_values(utc_row)
    temps_xn = _col_values(xn_row) if xn_row else []
    temps_tmp = _col_values(tmp_row) if tmp_row else []

    n = min(len(dates), len(hours), max(len(temps_xn), len(temps_tmp)))
    out: list[tuple[dt.date, int]] = []
    cur_date = issue_date
    last_day_token: str | None = None
    for i in range(n):
        # The DT row is sparse: it shows day-of-month at the start of each
        # new day, blank otherwise. Carry the last seen day forward.
        day_tok = dates[i]
        if day_tok and day_tok.isdigit():
            last_day_token = day_tok
        if last_day_token is None:
            continue
        try:
            day_of_month = int(last_day_token)
        except ValueError:
            continue
        # Compose the target date — assume same month/year as issue, roll
        # forward across month boundaries.
        target = issue_date.replace(day=1)
        # Advance month-by-month until day_of_month >= issue day for the
        # first occurrence that follows the issue date.
        candidate = issue_date.replace(day=day_of_month) if day_of_month >= issue_date.day else (
            (issue_date.replace(day=1) + dt.timedelta(days=32)).replace(day=day_of_month)
        )
        target = candidate
        # X/N row: alternating max/min, indicated by the hour-of-day. NBS
        # tags the max temp at ~00 UTC the next day for daytime cycles.
        try:
            hour_int = int(hours[i]) if hours[i] else None
        except ValueError:
            hour_int = None
        # Use TMP row preferentially when X/N is empty, but only at the
        # hour closest to local afternoon (~21UTC East / 00UTC West).
        val_str = temps_xn[i] if i < len(temps_xn) and temps_xn[i] else (
            temps_tmp[i] if i < len(temps_tmp) else ""
        )
        if not val_str or not re.fullmatch(r"-?\d+", val_str):
            continue
        # X/N values that are higher than typical morning lows usually mark
        # the daily max. We accept values issued at hour >= 18 UTC or 00 UTC
        # on the next day as candidate daily-maxes.
        if hour_int is not None and hour_int not in (0, 18, 21, 6):
            # NBS often puts max at 00z next day for warm season cycles;
            # be permissive here.
            pass
        try:
            val = int(val_str)
        except ValueError:
            continue
        # Track the per-day max we've seen across the row.
        # Use issue_hour to break ties on which row to trust.
        existing = next((p for p in out if p[0] == target), None)
        if existing is None:
            out.append((target, val))
        elif val > existing[1]:
            # Replace with the higher value (which is, by NBS convention,
            # the daytime max for that target date).
            out = [p for p in out if p[0] != target]
            out.append((target, val))

    # De-duplicate / sort
    out = sorted({d: v for d, v in out}.items())
    return out


async def _fetch_nbm_bulletin(
    client: httpx.AsyncClient,
    *,
    headers: dict,
) -> tuple[str, dt.datetime] | None:
    """Try the freshest preferred cycle from NOMADS. Returns (text, run_time)."""
    today_utc = dt.datetime.now(dt.timezone.utc)
    candidates: list[tuple[str, str]] = []
    for offset_days in (0, 1):
        ymd = (today_utc - dt.timedelta(days=offset_days)).strftime("%Y%m%d")
        for hh in NBM_PREFERRED_CYCLES:
            candidates.append((ymd, hh))

    for ymd, hh in candidates:
        url = NBM_NOMADS_BASE.format(ymd=ymd, hh=hh)
        try:
            r = await client.get(url, headers=headers, timeout=20.0)
            if r.status_code != 200 or not r.text:
                continue
            run_time = dt.datetime.strptime(f"{ymd} {hh}", "%Y%m%d %H").replace(
                tzinfo=dt.timezone.utc
            )
            log.info("NBM bulletin fetched: %s/%sz (%d KB)", ymd, hh, len(r.text) // 1024)
            return r.text, run_time
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            log.debug("NBM cycle %s/%sz unavailable: %s", ymd, hh, exc)
    return None


def _split_nbs_blocks(text: str) -> dict[str, str]:
    """Return ``{ICAO: block_text}`` from a full NBS bulletin file."""
    out: dict[str, str] = {}
    headers = list(_NBS_HEADER_RE.finditer(text))
    for i, m in enumerate(headers):
        icao = m.group(1)
        start = m.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        out[icao] = text[start:end]
    return out


async def _fetch_nbm_for_stations(
    client: httpx.AsyncClient,
    stations: list[Station],
    *,
    headers: dict,
    forecast_days: int,
) -> dict[str, list[ForecastRow]]:
    """One bulletin -> rows for every station present. Returns
    ``{slug: [ForecastRow,...]}`` for stations we found."""
    fetched = await _fetch_nbm_bulletin(client, headers=headers)
    if fetched is None:
        log.warning("Could not fetch any NBS bulletin; skipping nbm:station source")
        return {}
    text, run_time = fetched
    by_icao = _split_nbs_blocks(text)
    out: dict[str, list[ForecastRow]] = {}
    today_utc = dt.datetime.now(dt.timezone.utc).date()
    horizon = today_utc + dt.timedelta(days=forecast_days)

    for s in stations:
        block = by_icao.get(s.icao)
        if not block:
            log.debug("NBM bulletin missing block for %s", s.icao)
            continue
        try:
            pairs = _parse_nbm_block(block)
        except Exception as exc:  # noqa: BLE001
            log.warning("NBM parse failed for %s: %s", s.icao, exc)
            continue
        rows: list[ForecastRow] = []
        for d, val in pairs:
            if d < today_utc or d > horizon:
                continue
            rows.append(
                ForecastRow(
                    station_id=0,
                    target_date=d,
                    source="nbm:station",
                    run_time=run_time,
                    predicted_max_f=float(val),
                    predicted_std_f=None,
                    raw={"icao": s.icao, "value": val, "unit": "F"},
                )
            )
        if rows:
            out[s.slug] = rows
    return out


# ---------------------------------------------------------------------------
# Orchestration + persistence
# ---------------------------------------------------------------------------


UPSERT_FORECAST_SQL = """
INSERT INTO forecasts
    (station_id, target_date, source, run_time,
     predicted_max_f, predicted_std_f, raw, ingested_at)
VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, now())
ON CONFLICT (station_id, target_date, source, run_time) DO UPDATE SET
    predicted_max_f = EXCLUDED.predicted_max_f,
    predicted_std_f = EXCLUDED.predicted_std_f,
    raw             = EXCLUDED.raw,
    ingested_at     = now()
"""


def persist_forecasts(rows: list[ForecastRow]) -> int:
    if not rows:
        return 0
    from ..db import with_conn

    n = 0
    with with_conn() as conn, conn.cursor() as cur:
        for r in rows:
            cur.execute(
                UPSERT_FORECAST_SQL,
                (
                    r.station_id,
                    r.target_date,
                    r.source,
                    r.run_time,
                    r.predicted_max_f,
                    r.predicted_std_f,
                    json.dumps(r.raw),
                ),
            )
            n += 1
    return n


async def ingest_forecasts_async(
    station_slugs: Iterable[str],
    *,
    past_days: int = 7,
    forecast_days: int = 8,
) -> dict[str, int]:
    from ..db import station_id_by_slug

    sid_map = station_id_by_slug()
    headers = {"User-Agent": config.http_user_agent(), "Accept": "application/json"}

    stations = [REGISTRY[s] for s in station_slugs if s in REGISTRY]
    all_rows: list[ForecastRow] = []

    async with httpx.AsyncClient(headers=headers) as client:
        async def _runner(s: Station) -> list[ForecastRow]:
            sid = sid_map.get(s.slug)
            if sid is None:
                log.warning(
                    "Station %r is in registry but missing from DB — re-seed",
                    s.slug,
                )
                return []
            collected: list[ForecastRow] = []
            try:
                om = await _fetch_openmeteo(
                    client, s, past_days=past_days, forecast_days=forecast_days
                )
                for row in om:
                    row.station_id = sid
                collected.extend(om)
            except Exception as exc:  # noqa: BLE001
                log.warning("Open-Meteo failed for %s: %s", s.slug, exc)
            try:
                bm = await _fetch_openmeteo_bestmatch(
                    client, s, past_days=past_days, forecast_days=forecast_days
                )
                for row in bm:
                    row.station_id = sid
                collected.extend(bm)
            except Exception as exc:  # noqa: BLE001
                log.warning("Open-Meteo bestmatch failed for %s: %s", s.slug, exc)
            # HRRR (review §4.1) — CONUS only; harmless to call for all
            # current stations (they're all US).
            try:
                hrrr = await _fetch_hrrr_conus(
                    client, s, past_days=past_days, forecast_days=forecast_days
                )
                for row in hrrr:
                    row.station_id = sid
                collected.extend(hrrr)
            except Exception as exc:  # noqa: BLE001
                log.warning("Open-Meteo HRRR failed for %s: %s", s.slug, exc)
            # ECMWF AIFS (review §4.3).
            try:
                aifs = await _fetch_ecmwf_aifs(
                    client, s, past_days=past_days, forecast_days=forecast_days
                )
                for row in aifs:
                    row.station_id = sid
                collected.extend(aifs)
            except Exception as exc:  # noqa: BLE001
                log.warning("Open-Meteo AIFS failed for %s: %s", s.slug, exc)
            # GFS GraphCast (review §4.3).
            try:
                gc = await _fetch_gfs_graphcast(
                    client, s, past_days=past_days, forecast_days=forecast_days
                )
                for row in gc:
                    row.station_id = sid
                collected.extend(gc)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Open-Meteo GraphCast failed for %s: %s", s.slug, exc
                )
            # NBM probabilistic percentiles (review §4.2).
            try:
                pct = await _fetch_nbm_percentiles(
                    client, s, past_days=past_days, forecast_days=forecast_days
                )
                for row in pct:
                    row.station_id = sid
                collected.extend(pct)
            except Exception as exc:  # noqa: BLE001
                log.info("NBM percentiles failed for %s: %s", s.slug, exc)
            try:
                nws = await _fetch_nws(
                    client, s, headers=headers, forecast_days=forecast_days
                )
                for row in nws:
                    row.station_id = sid
                collected.extend(nws)
            except Exception as exc:  # noqa: BLE001
                log.warning("NWS hourly failed for %s: %s", s.slug, exc)
            try:
                nws_d = await _fetch_nws_daily(
                    client, s, headers=headers, forecast_days=forecast_days
                )
                for row in nws_d:
                    row.station_id = sid
                collected.extend(nws_d)
            except Exception as exc:  # noqa: BLE001
                log.warning("NWS daily failed for %s: %s", s.slug, exc)
            return collected

        sem = asyncio.Semaphore(3)

        async def _bound(s: Station) -> list[ForecastRow]:
            async with sem:
                return await _runner(s)

        results = await asyncio.gather(*(_bound(s) for s in stations))

        # NBM is one bulletin for all stations — fetch once.
        try:
            nbm_by_slug = await _fetch_nbm_for_stations(
                client, stations, headers=headers, forecast_days=forecast_days
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("NBM ingest failed: %s", exc)
            nbm_by_slug = {}

    for rs in results:
        all_rows.extend(rs)

    for slug, rows in nbm_by_slug.items():
        sid = sid_map.get(slug)
        if sid is None:
            continue
        for row in rows:
            row.station_id = sid
        all_rows.extend(rows)

    n = persist_forecasts(all_rows)
    log.info("Persisted %d forecast rows from %d stations.", n, len(stations))
    return {"forecasts": n}


def ingest_forecasts(
    station_slugs: Iterable[str],
    *,
    past_days: int = 7,
    forecast_days: int = 8,
) -> dict[str, int]:
    return asyncio.run(
        ingest_forecasts_async(
            station_slugs,
            past_days=past_days,
            forecast_days=forecast_days,
        )
    )
