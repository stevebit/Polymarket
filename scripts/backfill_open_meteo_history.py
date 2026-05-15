"""Backfill historical forecasts via Open-Meteo's Historical Forecast API.

Plan Phase 4 (review §4.1 / §4.3): rebuild the ``forecasts`` table for the
Phase 3 sources (HRRR, AIFS, GraphCast, NBM, bestmatch) plus the legacy
GFS/IFS/ICON/GEM, anchored to the *archive* endpoint so we can fairly
calibrate and backtest 5+ months of honest history.

This script is **destructive only in the sense of upserting** — it does
not delete anything. Run order is documented in
``.cursor/plans/weather_pipeline_fix_and_improvement_*.plan.md``:

    1. ``scripts/reset_for_rebuild.py`` (Phase 5) → clear derived tables.
    2. ``scripts/backfill_open_meteo_history.py`` → THIS script.
    3. ``python -m polymarket_weather.cli.fit_postprocess ...``
       (Phase 4 step 2) → refit EMOS on the extended sources.
    4. ``python -m polymarket_weather.cli.predict_history ...``
       (Phase 4 step 3) → produce ``bucket_probs`` rows with honest
       historical ``run_time``.

Endpoint reference:

    https://archive-api.open-meteo.com/v1/archive   (reanalysis-style)
    https://historical-forecast-api.open-meteo.com/v1/forecast
      ?...&models=<model>&past_days=<N>
      ?...&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD

Open-Meteo's *Historical Forecast API* preserves the actual forecast cycle
each model issued each day (i.e. zero leakage). That's the right source
for fair retrospective backtesting.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from polymarket_weather import config  # noqa: E402
from polymarket_weather.data.forecasts import (  # noqa: E402
    ForecastRow,
    persist_forecasts,
    _get_with_retry,
    model_cycle_at,
)
from polymarket_weather.cli._common import parse_cli_date  # noqa: E402
from polymarket_weather.db import station_id_by_slug  # noqa: E402
from polymarket_weather.stations import REGISTRY, Station  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

log = logging.getLogger("backfill_open_meteo_history")


HISTORICAL_FORECAST_URL = (
    "https://historical-forecast-api.open-meteo.com/v1/forecast"
)


@dataclass(frozen=True)
class HistoricalSource:
    """One row in the source registry consumed by this script."""

    source_name: str
    open_meteo_model: str
    # Use ``daily=temperature_2m_max`` (False) or aggregate from hourly (True).
    hourly: bool


HISTORICAL_SOURCES: tuple[HistoricalSource, ...] = (
    # Existing global deterministic (re-anchored to the historical endpoint
    # so we get the *actual* per-day cycle data, not the rolling forecast).
    HistoricalSource("openmeteo:gfs_seamless", "gfs_seamless", False),
    HistoricalSource("openmeteo:ecmwf_ifs04", "ecmwf_ifs04", False),
    HistoricalSource("openmeteo:icon_seamless", "icon_seamless", False),
    HistoricalSource("openmeteo:gem_seamless", "gem_seamless", False),
    HistoricalSource("openmeteo:bestmatch", "bestmatch", False),
    # Phase 3 sources.
    # Historical Forecast host expects ``gfs_hrrr`` (``hrrr_conus`` 400s there).
    HistoricalSource("openmeteo:hrrr_conus", "gfs_hrrr", True),
    HistoricalSource("openmeteo:aifs025_single", "ecmwf_aifs025_single", False),
    HistoricalSource("openmeteo:gfs_graphcast025", "gfs_graphcast025", True),
)


async def _fetch_archive_block(
    client: httpx.AsyncClient,
    station: Station,
    source: HistoricalSource,
    start_date: dt.date,
    end_date: dt.date,
) -> list[ForecastRow]:
    common = {
        "latitude": station.lat,
        "longitude": station.lon,
        "temperature_unit": "fahrenheit",
        "timezone": station.tz,
        "models": source.open_meteo_model,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }
    if source.open_meteo_model == "bestmatch":
        # ``bestmatch`` is the default when ``models=`` is omitted.
        params = {k: v for k, v in common.items() if k != "models"}
    else:
        params = common
    if source.hourly:
        params["hourly"] = "temperature_2m"
    else:
        params["daily"] = "temperature_2m_max"

    try:
        r = await _get_with_retry(client, HISTORICAL_FORECAST_URL, params=params)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Archive %s/%s [%s..%s] failed: %s",
            station.slug, source.source_name, start_date, end_date, exc,
        )
        return []
    j = r.json()

    rows: list[ForecastRow] = []
    if not source.hourly:
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
            # Use the most recent global cycle anchored at the target day's
            # 00 UTC — preserves "issued in the morning of target_date"
            # semantics for cycles like 00z/06z.
            run_anchor = dt.datetime.combine(
                tdate, dt.time(0, 0), tzinfo=dt.timezone.utc
            )
            run_time = model_cycle_at(run_anchor, source.open_meteo_model)
            rows.append(
                ForecastRow(
                    station_id=0,
                    target_date=tdate,
                    source=source.source_name,
                    run_time=run_time,
                    predicted_max_f=float(v),
                    predicted_std_f=None,
                    raw={
                        "model": source.open_meteo_model,
                        "value": v,
                        "unit": "F",
                        "archive": True,
                    },
                )
            )
        return rows

    # Hourly aggregation to local-day max.
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
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=tz)
        local = ts.astimezone(tz)
        bucket.setdefault(local.date(), []).append(float(v))

    for d, vals in sorted(bucket.items()):
        if not vals:
            continue
        run_anchor = dt.datetime.combine(
            d, dt.time(0, 0), tzinfo=dt.timezone.utc
        )
        run_time = model_cycle_at(run_anchor, source.open_meteo_model)
        rows.append(
            ForecastRow(
                station_id=0,
                target_date=d,
                source=source.source_name,
                run_time=run_time,
                predicted_max_f=max(vals),
                predicted_std_f=None,
                raw={
                    "model": source.open_meteo_model,
                    "n_hours": len(vals),
                    "max": max(vals),
                    "archive": True,
                },
            )
        )
    return rows


def _date_chunks(
    start: dt.date, end: dt.date, *, days: int = 31
) -> Iterable[tuple[dt.date, dt.date]]:
    """Yield ``(start, end)`` sub-ranges no longer than ``days`` calendar
    days. Keeps Open-Meteo request size manageable."""
    cur = start
    while cur <= end:
        nxt = min(cur + dt.timedelta(days=days - 1), end)
        yield cur, nxt
        cur = nxt + dt.timedelta(days=1)


async def run_backfill(
    station_slugs: list[str],
    start_date: dt.date,
    end_date: dt.date,
    *,
    sources: tuple[HistoricalSource, ...] | None = None,
    chunk_days: int = 31,
    concurrency: int = 3,
) -> dict:
    headers = {"User-Agent": config.http_user_agent(), "Accept": "application/json"}
    sid_map = station_id_by_slug()

    sem = asyncio.Semaphore(concurrency)

    async def _one(client, station, src, lo, hi):
        async with sem:
            rows = await _fetch_archive_block(client, station, src, lo, hi)
            sid = sid_map.get(station.slug)
            if sid is None:
                return 0
            for r in rows:
                r.station_id = sid
            return persist_forecasts(rows)

    written = 0
    stations = [REGISTRY[s] for s in station_slugs if s in REGISTRY]
    srcs = sources if sources is not None else HISTORICAL_SOURCES
    async with httpx.AsyncClient(headers=headers, timeout=120.0) as client:
        tasks = []
        for station in stations:
            for src in srcs:
                for lo, hi in _date_chunks(start_date, end_date, days=chunk_days):
                    tasks.append(_one(client, station, src, lo, hi))
        results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, Exception):
            log.warning("Task error: %s", r)
            continue
        written += int(r)
    return {"written": written, "tasks": len(results), "stations": len(stations)}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--start",
        type=parse_cli_date,
        default=dt.date(2025, 11, 1),
        help="Inclusive start date: YYYY-MM-DD or today/yesterday (UTC). Default 2025-11-01.",
    )
    p.add_argument(
        "--end",
        type=parse_cli_date,
        default=dt.datetime.now(dt.timezone.utc).date(),
        help="Inclusive end date: YYYY-MM-DD or today/yesterday (UTC). Default today UTC.",
    )
    p.add_argument(
        "--station",
        default="",
        help="Comma-separated station slugs (default: all in REGISTRY).",
    )
    p.add_argument("--chunk-days", type=int, default=31)
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument(
        "--sources",
        default="",
        help=(
            "Comma-separated HistoricalSource names to fetch (e.g. "
            "openmeteo:hrrr_conus). Empty = all registered sources."
        ),
    )
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    slugs = (
        [s.strip() for s in args.station.split(",") if s.strip()]
        if args.station
        else list(REGISTRY.keys())
    )

    want: tuple[HistoricalSource, ...] | None = None
    if args.sources.strip():
        names = {s.strip() for s in args.sources.split(",") if s.strip()}
        filt = tuple(s for s in HISTORICAL_SOURCES if s.source_name in names)
        missing = names - {s.source_name for s in filt}
        if missing:
            raise SystemExit(f"Unknown --sources names: {sorted(missing)}")
        want = filt

    result = asyncio.run(
        run_backfill(
            slugs, args.start, args.end,
            sources=want,
            chunk_days=args.chunk_days, concurrency=args.concurrency,
        )
    )
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
