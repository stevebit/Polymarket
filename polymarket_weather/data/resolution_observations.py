"""Weather Underground / weather.com resolution-side observations ingest.

Polymarket daily temperature markets resolve from Wunderground station pages.
Those pages are backed by weather.com historical observations for the ICAO
station code in the market resolution source URL.

This module fetches that same historical stream, aggregates to per-local-day
max temperature, and upserts rows into ``observations`` with source:
``wunderground:historical``.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Iterable
from zoneinfo import ZoneInfo

import httpx

from ..stations import REGISTRY, Station
from .observations import ObservationRow, persist_observations

log = logging.getLogger(__name__)

WU_HIST_URL = "https://api.weather.com/v1/location/{location}/observations/historical.json"
WU_DEFAULT_API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"
WU_MAX_RANGE_DAYS = 31
WU_SOURCE = "wunderground:historical"
FINALIZATION_WINDOW_DAYS = 2
WU_MIN_F = -80
WU_MAX_F = 130


@dataclass(frozen=True)
class DailyMax:
    obs_date: dt.date
    max_f: int


def _month_chunks(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    out: list[tuple[dt.date, dt.date]] = []
    cur = start
    while cur <= end:
        chunk_end = min(end, cur + dt.timedelta(days=WU_MAX_RANGE_DAYS - 1))
        out.append((cur, chunk_end))
        cur = chunk_end + dt.timedelta(days=1)
    return out


def _fetch_chunk(
    client: httpx.Client,
    station: Station,
    start: dt.date,
    end: dt.date,
    *,
    api_key: str,
) -> list[dict]:
    location = f"{station.icao}:9:US"
    params = {
        "apiKey": api_key,
        "units": "e",
        "startDate": start.strftime("%Y%m%d"),
        "endDate": end.strftime("%Y%m%d"),
    }
    r = client.get(
        WU_HIST_URL.format(location=location),
        params=params,
        timeout=30.0,
    )
    if r.status_code == 400:
        # weather.com returns 400 for windows that are too large / invalid.
        log.warning(
            "WU 400 for %s [%s..%s], skipping chunk",
            station.slug,
            start,
            end,
        )
        return []
    r.raise_for_status()
    payload = r.json()
    obs = payload.get("observations")
    if not isinstance(obs, list):
        return []
    return obs


def _daily_max_from_obs(station: Station, observations: list[dict]) -> list[DailyMax]:
    tz = ZoneInfo(station.tz)
    by_date: dict[dt.date, int] = {}
    for row in observations:
        temp = row.get("temp")
        ts = row.get("valid_time_gmt")
        if temp is None or ts is None:
            continue
        try:
            temp_i = int(round(float(temp)))
            ts_i = int(ts)
        except (TypeError, ValueError):
            continue
        if temp_i < WU_MIN_F or temp_i > WU_MAX_F:
            # Rare bad spikes exist in the upstream feed (e.g., 160F values).
            continue
        local_dt = dt.datetime.fromtimestamp(ts_i, tz=dt.timezone.utc).astimezone(tz)
        d = local_dt.date()
        prev = by_date.get(d)
        if prev is None or temp_i > prev:
            by_date[d] = temp_i
    return [DailyMax(obs_date=d, max_f=mx) for d, mx in sorted(by_date.items())]


def _fetch_station_daily_maxes(
    client: httpx.Client,
    station: Station,
    start: dt.date,
    end: dt.date,
    *,
    api_key: str,
) -> list[DailyMax]:
    all_obs: list[dict] = []
    for chunk_start, chunk_end in _month_chunks(start, end):
        all_obs.extend(
            _fetch_chunk(
                client,
                station,
                chunk_start,
                chunk_end,
                api_key=api_key,
            )
        )
    return _daily_max_from_obs(station, all_obs)


def ingest_resolution_observations(
    station_slugs: Iterable[str],
    start: dt.date,
    end: dt.date,
    *,
    api_key: str | None = None,
) -> dict[str, int]:
    from ..db import station_id_by_slug

    sid_map = station_id_by_slug()
    token = api_key or WU_DEFAULT_API_KEY
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
    total_rows = 0
    today = dt.date.today()

    with httpx.Client(headers=headers) as client:
        for slug in station_slugs:
            station = REGISTRY.get(slug)
            if station is None:
                log.warning("Unknown station slug %r — skipping", slug)
                continue
            sid = sid_map.get(slug)
            if sid is None:
                log.warning("station_id missing for %s — run migrate", slug)
                continue
            try:
                daily_maxes = _fetch_station_daily_maxes(
                    client,
                    station,
                    start,
                    end,
                    api_key=token,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("WU fetch failed for %s: %s", slug, exc)
                continue
            rows = [
                ObservationRow(
                    station_id=sid,
                    obs_date=d.obs_date,
                    source=WU_SOURCE,
                    observed_max_f=d.max_f,
                    finalized=(today - d.obs_date).days >= FINALIZATION_WINDOW_DAYS,
                )
                for d in daily_maxes
            ]
            n = persist_observations(rows)
            total_rows += n
            log.info("Persisted %d WU observations for %s", n, slug)

    return {"observations": total_rows}
