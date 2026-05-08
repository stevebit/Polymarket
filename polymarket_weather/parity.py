"""NOAA vs Wunderground parity metrics for Polymarket resolution stations."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from typing import Sequence

from . import config
from .db import with_conn

NOAA_SOURCE = "noaa:ghcnd"
WU_SOURCE = "wunderground:historical"


@dataclass(frozen=True)
class StationParity:
    slug: str
    station_name: str
    n: int
    exact_match_rate: float
    mean_abs_error: float
    median_abs_error: float
    p95_abs_error: float
    max_abs_error: float
    mean_signed_error: float


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if q <= 0:
        return float(sorted_vals[0])
    if q >= 1:
        return float(sorted_vals[-1])
    idx = int(round((len(sorted_vals) - 1) * q))
    return float(sorted_vals[idx])


def compute_station_parity(lookback_days: int = 365) -> list[StationParity]:
    cutoff = dt.date.today() - dt.timedelta(days=lookback_days)
    sql = """
        SELECT
            s.slug,
            s.display_name,
            n.obs_date,
            n.observed_max_f::float AS noaa_f,
            w.observed_max_f::float AS wu_f
        FROM observations n
        JOIN observations w
          ON w.station_id = n.station_id
         AND w.obs_date = n.obs_date
        JOIN stations s
          ON s.station_id = n.station_id
        WHERE n.source = %s
          AND w.source = %s
          AND n.obs_date >= %s
        ORDER BY s.slug, n.obs_date
    """
    by_station: dict[tuple[str, str], list[tuple[float, float]]] = {}
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (NOAA_SOURCE, WU_SOURCE, cutoff))
        for slug, name, _obs_date, noaa_f, wu_f in cur.fetchall():
            by_station.setdefault((slug, name), []).append((float(noaa_f), float(wu_f)))

    out: list[StationParity] = []
    for (slug, name), vals in sorted(by_station.items()):
        if not vals:
            continue
        diffs = [wu - noaa for noaa, wu in vals]
        abs_diffs = sorted(abs(d) for d in diffs)
        n = len(vals)
        exact = sum(1 for d in diffs if d == 0.0) / n
        out.append(
            StationParity(
                slug=slug,
                station_name=name,
                n=n,
                exact_match_rate=exact,
                mean_abs_error=mean(abs_diffs),
                median_abs_error=median(abs_diffs),
                p95_abs_error=_percentile(abs_diffs, 0.95),
                max_abs_error=abs_diffs[-1],
                mean_signed_error=mean(diffs),
            )
        )
    return out


def render_parity_markdown(rows: list[StationParity], lookback_days: int) -> str:
    today = dt.datetime.now(dt.timezone.utc).isoformat()
    lines: list[str] = []
    lines.append("# NOAA vs Wunderground parity")
    lines.append("")
    lines.append(f"- Generated UTC: `{today}`")
    lines.append(f"- Lookback days: `{lookback_days}`")
    lines.append(f"- NOAA source: `{NOAA_SOURCE}`")
    lines.append(f"- Wunderground source: `{WU_SOURCE}`")
    lines.append("")
    if not rows:
        lines.append("No overlapping NOAA/WU observations found in window.")
        return "\n".join(lines) + "\n"

    lines.append(
        "| station | n | exact_match | mean_abs | median_abs | p95_abs | max_abs | mean_signed (WU-NOAA) |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        lines.append(
            f"| {r.slug} | {r.n} | {100*r.exact_match_rate:.1f}% | "
            f"{r.mean_abs_error:.2f} | {r.median_abs_error:.2f} | {r.p95_abs_error:.2f} | "
            f"{r.max_abs_error:.2f} | {r.mean_signed_error:.2f} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def write_parity_report(markdown: str, *, out_path: Path | None = None) -> Path:
    if out_path is None:
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = config.paths().reports / f"parity_{ts}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    return out_path
