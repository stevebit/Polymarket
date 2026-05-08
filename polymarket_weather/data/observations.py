"""NOAA GHCN-Daily observations ingest.

Uses the NOAA Climate Data Online v2 API (``ncdc.noaa.gov/cdo-web/api/v2``) with
the user's free token (env var ``NOAA_Token_ID``). TMAX values arrive in tenths
of a degree Celsius — we convert to whole degrees Fahrenheit, matching the
Polymarket resolution rule (rounded integer F).

Rate limits: NOAA enforces 5 requests/sec and 10000/day per token. We page in
1000-row chunks, sleep ~0.25s between calls, and back off with jitter on 429.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Iterable

import httpx

from .. import config
from ..stations import REGISTRY, Station

log = logging.getLogger(__name__)

NOAA_BASE = "https://www.ncdc.noaa.gov/cdo-web/api/v2/data"
PAGE_LIMIT = 1000
FINALIZATION_WINDOW_DAYS = 5  # Mark obs older than this as ``finalized``


@dataclass
class ObservationRow:
    station_id: int
    obs_date: dt.date
    source: str
    observed_max_f: int
    finalized: bool


def _tenths_c_to_f(value: int) -> int:
    """NOAA returns TMAX in tenths of °C. Resolution is per-integer F."""
    c = value / 10.0
    f = c * 9.0 / 5.0 + 32.0
    return int(round(f))


def _request_page(
    client: httpx.Client,
    station: Station,
    start: dt.date,
    end: dt.date,
    offset: int,
    *,
    headers: dict,
) -> dict:
    params = {
        "datasetid": "GHCND",
        "stationid": f"GHCND:{station.ghcn_id}",
        "datatypeid": "TMAX",
        "startdate": start.isoformat(),
        "enddate": end.isoformat(),
        "limit": PAGE_LIMIT,
        "offset": offset,
        # Intentionally no ``units`` parameter — we want the raw GHCN-D values
        # (TMAX in tenths of a degree Celsius) and convert ourselves so the
        # rounding matches Polymarket's whole-°F resolution rule.
    }
    backoff = 1.0
    for attempt in range(5):
        r = client.get(NOAA_BASE, params=params, headers=headers, timeout=30.0)
        if r.status_code == 200:
            try:
                return r.json()
            except json.JSONDecodeError:
                return {}
        if r.status_code in (429, 500, 502, 503, 504):
            sleep_for = backoff + random.uniform(0, 0.5)
            log.warning(
                "NOAA %s for %s offset=%d, sleeping %.1fs (attempt %d)",
                r.status_code, station.slug, offset, sleep_for, attempt + 1,
            )
            time.sleep(sleep_for)
            backoff *= 2
            continue
        r.raise_for_status()
    raise RuntimeError(
        f"NOAA repeatedly failed for {station.slug} offset={offset}"
    )


def _fetch_station(
    client: httpx.Client,
    station: Station,
    start: dt.date,
    end: dt.date,
    *,
    headers: dict,
) -> list[ObservationRow]:
    """Page NOAA results for [start, end] inclusive. NOAA caps each request to 1y."""
    out: list[ObservationRow] = []
    cur_start = start
    today = dt.date.today()

    from ..db import station_id_by_slug

    sid = station_id_by_slug().get(station.slug)
    if sid is None:
        log.warning("station_id missing for %s — re-seed", station.slug)
        return []

    while cur_start <= end:
        # NOAA requests must span <= 1 year. Use 365-day windows.
        window_end = min(end, cur_start + dt.timedelta(days=365))

        offset = 1
        while True:
            payload = _request_page(
                client, station, cur_start, window_end, offset, headers=headers
            )
            results = payload.get("results") or []
            for r in results:
                date_str = r.get("date")
                value = r.get("value")
                if not date_str or value is None:
                    continue
                try:
                    obs_date = dt.date.fromisoformat(date_str.split("T")[0])
                except ValueError:
                    continue
                f_val = _tenths_c_to_f(int(value))
                finalized = (today - obs_date).days >= FINALIZATION_WINDOW_DAYS
                out.append(
                    ObservationRow(
                        station_id=sid,
                        obs_date=obs_date,
                        source="noaa:ghcnd",
                        observed_max_f=f_val,
                        finalized=finalized,
                    )
                )

            meta = payload.get("metadata") or {}
            res = meta.get("resultset") or {}
            total = int(res.get("count", 0))
            if not results or offset + PAGE_LIMIT > total:
                break
            offset += PAGE_LIMIT
            time.sleep(0.25)

        cur_start = window_end + dt.timedelta(days=1)
        time.sleep(0.25)

    return out


UPSERT_OBS_SQL = """
INSERT INTO observations
    (station_id, obs_date, source, observed_max_f, finalized, ingested_at)
VALUES (%s, %s, %s, %s, %s, now())
ON CONFLICT (station_id, obs_date, source) DO UPDATE SET
    observed_max_f = EXCLUDED.observed_max_f,
    finalized      = EXCLUDED.finalized,
    ingested_at    = now()
"""


def persist_observations(rows: list[ObservationRow]) -> int:
    if not rows:
        return 0
    from ..db import with_conn

    with with_conn() as conn, conn.cursor() as cur:
        cur.executemany(
            UPSERT_OBS_SQL,
            [
                (
                    r.station_id,
                    r.obs_date,
                    r.source,
                    r.observed_max_f,
                    r.finalized,
                )
                for r in rows
            ],
        )
        return cur.rowcount or len(rows)


def ingest_observations(
    station_slugs: Iterable[str],
    start: dt.date,
    end: dt.date,
) -> dict[str, int]:
    headers = {
        "token": config.noaa_token(),
        "User-Agent": config.http_user_agent(),
        "Accept": "application/json",
    }
    n_total = 0
    with httpx.Client(headers=headers) as client:
        for slug in station_slugs:
            station = REGISTRY.get(slug)
            if station is None:
                log.warning("Unknown station slug %r — skipping", slug)
                continue
            try:
                rows = _fetch_station(client, station, start, end, headers=headers)
            except Exception as exc:  # noqa: BLE001
                log.warning("NOAA fetch failed for %s: %s", slug, exc)
                continue
            n = persist_observations(rows)
            log.info("Persisted %d observations for %s", n, slug)
            n_total += n
            time.sleep(0.5)
    return {"observations": n_total}
