"""Per-station day-of-year climatology of TMAX-F.

Computed from the ``observations`` table over a configurable lookback window
(default ten years). UPSERTs into ``climatology(station_id, doy)``.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Iterable

from ..db import station_id_by_slug, with_conn
from ..stations import REGISTRY

log = logging.getLogger(__name__)


REFRESH_SQL = """
WITH src AS (
    SELECT
        station_id,
        EXTRACT(DOY FROM obs_date)::SMALLINT AS doy,
        observed_max_f::float8           AS f
    FROM observations
    WHERE station_id = %s
      AND obs_date >= %s
      AND observed_max_f IS NOT NULL
),
agg AS (
    SELECT
        station_id,
        doy,
        AVG(f)        AS tmax_mean,
        STDDEV_SAMP(f) AS tmax_std,
        COUNT(*)       AS sample_n
    FROM src
    GROUP BY station_id, doy
)
INSERT INTO climatology
    (station_id, doy, tmax_mean, tmax_std, sample_n, refreshed_at)
SELECT station_id, doy, tmax_mean, tmax_std, sample_n, now()
FROM agg
ON CONFLICT (station_id, doy) DO UPDATE SET
    tmax_mean    = EXCLUDED.tmax_mean,
    tmax_std     = EXCLUDED.tmax_std,
    sample_n     = EXCLUDED.sample_n,
    refreshed_at = now()
"""


def refresh_climatology(
    station_slugs: Iterable[str],
    *,
    lookback_days: int = 365 * 10,
) -> dict[str, int]:
    """Recompute climatology for the requested stations. Returns total rows touched."""
    sid_map = station_id_by_slug()
    cutoff = dt.date.today() - dt.timedelta(days=lookback_days)
    n = 0
    with with_conn() as conn, conn.cursor() as cur:
        for slug in station_slugs:
            if slug not in REGISTRY:
                log.warning("Unknown station slug %r — skipping", slug)
                continue
            sid = sid_map.get(slug)
            if sid is None:
                log.warning("Station %r missing in DB — skipping", slug)
                continue
            cur.execute(REFRESH_SQL, (sid, cutoff))
            n += cur.rowcount or 0
            log.info("Refreshed climatology rows for %s", slug)
    return {"climatology": n}
