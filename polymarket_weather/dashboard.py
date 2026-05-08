"""Standalone HTML dashboard for raw weather data exploration.

The dashboard is intentionally static: one generated HTML file with embedded
JSON so it can be opened locally without running a web server.

Features:
- US map with station markers (Leaflet)
- Click marker to switch active station
- Timeseries chart (Plotly) overlaying:
  - NOAA observations (raw observed_max_f)
  - Forecasts by source (openmeteo:* and nws:gridpoint)
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from .db import with_conn


def _to_float(value: Decimal | float | int | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _fetch_station_metadata(station_slugs: list[str]) -> list[dict]:
    sql = """
        SELECT station_id, slug, display_name, icao, lat, lon
        FROM stations
        WHERE slug = ANY(%s::text[])
        ORDER BY slug
    """
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (station_slugs,))
        rows = cur.fetchall()
    return [
        {
            "station_id": r[0],
            "slug": r[1],
            "display_name": r[2],
            "icao": r[3],
            "lat": _to_float(r[4]),
            "lon": _to_float(r[5]),
        }
        for r in rows
    ]


def _fetch_observations(station_ids: list[int], cutoff_date: dt.date) -> dict[int, list[dict]]:
    sql = """
        SELECT station_id, obs_date, source, observed_max_f, finalized, ingested_at
        FROM observations
        WHERE station_id = ANY(%s::int[])
          AND obs_date >= %s
        ORDER BY station_id, obs_date
    """
    out: dict[int, list[dict]] = {sid: [] for sid in station_ids}
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (station_ids, cutoff_date))
        for station_id, obs_date, source, observed_max_f, finalized, ingested_at in cur.fetchall():
            out.setdefault(station_id, []).append(
                {
                    "date": obs_date.isoformat(),
                    "source": source,
                    "value_f": _to_float(observed_max_f),
                    "finalized": bool(finalized),
                    "ingested_at": ingested_at.isoformat() if ingested_at else None,
                }
            )
    return out


def _fetch_forecasts(
    station_ids: list[int],
    cutoff_date: dt.date,
    *,
    all_runs: bool,
) -> dict[int, list[dict]]:
    if all_runs:
        sql = """
            SELECT station_id, target_date, source, run_time, predicted_max_f, predicted_std_f, ingested_at
            FROM forecasts
            WHERE station_id = ANY(%s::int[])
              AND target_date >= %s
            ORDER BY station_id, source, run_time, target_date
        """
        params = (station_ids, cutoff_date)
    else:
        sql = """
            WITH latest AS (
                SELECT
                    station_id,
                    target_date,
                    source,
                    run_time,
                    predicted_max_f,
                    predicted_std_f,
                    ingested_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY station_id, target_date, source
                        ORDER BY run_time DESC
                    ) AS rn
                FROM forecasts
                WHERE station_id = ANY(%s::int[])
                  AND target_date >= %s
            )
            SELECT station_id, target_date, source, run_time, predicted_max_f, predicted_std_f, ingested_at
            FROM latest
            WHERE rn = 1
            ORDER BY station_id, source, target_date
        """
        params = (station_ids, cutoff_date)

    out: dict[int, list[dict]] = {sid: [] for sid in station_ids}
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        for (
            station_id,
            target_date,
            source,
            run_time,
            predicted_max_f,
            predicted_std_f,
            ingested_at,
        ) in cur.fetchall():
            out.setdefault(station_id, []).append(
                {
                    "target_date": target_date.isoformat(),
                    "source": source,
                    "run_time": run_time.isoformat() if run_time else None,
                    "predicted_max_f": _to_float(predicted_max_f),
                    "predicted_std_f": _to_float(predicted_std_f),
                    "ingested_at": ingested_at.isoformat() if ingested_at else None,
                }
            )
    return out


def _fetch_polymarket_markets(station_ids: list[int]) -> dict[int, list[dict]]:
    sql = """
        SELECT station_id, event_slug, target_date, gamma_event_id, raw, fetched_at
        FROM pm_events
        WHERE station_id = ANY(%s::int[])
        ORDER BY station_id, target_date DESC
    """
    out: dict[int, list[dict]] = {sid: [] for sid in station_ids}
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (station_ids,))
        for station_id, event_slug, target_date, gamma_event_id, raw, fetched_at in cur.fetchall():
            raw_obj = raw if isinstance(raw, dict) else {}
            out.setdefault(station_id, []).append(
                {
                    "event_slug": event_slug,
                    "target_date": target_date.isoformat() if target_date else None,
                    "gamma_event_id": str(gamma_event_id) if gamma_event_id is not None else None,
                    "title": raw_obj.get("title") or event_slug,
                    "active": bool(raw_obj.get("active")),
                    "closed": bool(raw_obj.get("closed")),
                    "fetched_at": fetched_at.isoformat() if fetched_at else None,
                    "url": f"https://polymarket.com/event/{event_slug}",
                }
            )
    return out


def _compose_dataset(
    station_slugs: Iterable[str],
    *,
    lookback_days: int,
    all_forecast_runs: bool,
) -> dict:
    wanted = [s for s in station_slugs if s]
    station_meta = _fetch_station_metadata(wanted)
    station_ids = [s["station_id"] for s in station_meta]
    cutoff = dt.date.today() - dt.timedelta(days=max(1, lookback_days))

    obs_by_sid = _fetch_observations(station_ids, cutoff)
    fc_by_sid = _fetch_forecasts(station_ids, cutoff, all_runs=all_forecast_runs)
    markets_by_sid = _fetch_polymarket_markets(station_ids)

    stations: list[dict] = []
    for s in station_meta:
        sid = s["station_id"]
        stations.append(
            {
                "slug": s["slug"],
                "display_name": s["display_name"],
                "icao": s["icao"],
                "lat": s["lat"],
                "lon": s["lon"],
                "observations": obs_by_sid.get(sid, []),
                "forecasts": fc_by_sid.get(sid, []),
                "markets": markets_by_sid.get(sid, []),
            }
        )

    total_markets = sum(len(s["markets"]) for s in stations)
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "lookback_days": lookback_days,
        "all_forecast_runs": all_forecast_runs,
        "station_count": len(stations),
        "market_count": total_markets,
        "stations": stations,
    }


def _build_html(dataset: dict) -> str:
    payload = json.dumps(dataset, separators=(",", ":"))
    html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Polymarket Weather Raw Data Dashboard</title>
  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
    crossorigin=""
  />
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <script
    src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
    crossorigin=""
  ></script>
  <style>
    body {{
      margin: 0;
      font-family: Arial, sans-serif;
      background: #0b1020;
      color: #e6edf7;
    }}
    .header {{
      padding: 12px 16px;
      border-bottom: 1px solid #27314f;
      display: flex;
      flex-wrap: wrap;
      gap: 16px;
      align-items: center;
    }}
    .header h1 {{
      margin: 0;
      font-size: 18px;
    }}
    .meta {{
      color: #9eb0d1;
      font-size: 13px;
    }}
    .layout {{
      display: grid;
      grid-template-columns: 40% 60%;
      height: calc(100vh - 56px);
    }}
    #map {{
      height: 100%;
      width: 100%;
    }}
    .right {{
      display: grid;
      grid-template-rows: auto 1fr;
      border-left: 1px solid #27314f;
    }}
    .station-panel {{
      padding: 10px 14px;
      border-bottom: 1px solid #27314f;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .station-panel select {{
      background: #111933;
      color: #e6edf7;
      border: 1px solid #33406a;
      padding: 6px 8px;
      border-radius: 6px;
    }}
    #chart {{
      width: 100%;
      height: 100%;
    }}
    @media (max-width: 980px) {{
      .layout {{
        grid-template-columns: 1fr;
        grid-template-rows: 45% 55%;
      }}
      .right {{
        border-left: none;
        border-top: 1px solid #27314f;
      }}
    }}
  </style>
</head>
<body>
  <div class="header">
    <h1>Polymarket Weather Raw Data Dashboard</h1>
    <div class="meta" id="meta"></div>
  </div>
  <div class="layout">
    <div id="map"></div>
    <div class="right">
      <div class="station-panel">
        <div id="stationMeta"></div>
        <label>
          Station:
          <select id="stationSelect"></select>
        </label>
      </div>
      <div id="chart"></div>
    </div>
  </div>

  <script>
    const DATA = __PAYLOAD__;
    const stationMap = new Map(DATA.stations.map(s => [s.slug, s]));
    const stationSelect = document.getElementById("stationSelect");
    const stationMeta = document.getElementById("stationMeta");

    document.getElementById("meta").textContent =
      `Generated UTC: ${DATA.generated_at_utc} | Stations: ${DATA.station_count} | Markets: ${DATA.market_count} | Lookback days: ${DATA.lookback_days}`;

    for (const st of DATA.stations) {{
      const opt = document.createElement("option");
      opt.value = st.slug;
      opt.textContent = `${{st.display_name}} (${{st.icao}})`;
      stationSelect.appendChild(opt);
    }}

    const map = L.map("map").setView([39.5, -98.35], 4);
    L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      maxZoom: 12,
      attribution: "&copy; OpenStreetMap contributors"
    }}).addTo(map);

    const markers = [];

    function setActiveMarker(slug) {{
      for (const m of markers) {{
        const isActive = m.slug === slug;
        m.marker.setStyle({{
          opacity: isActive ? 1 : 0.65,
          fillOpacity: isActive ? 0.9 : 0.45
        }});
      }}
    }}

    function buildMarketsPopup(st) {{
      if (!st.markets || st.markets.length === 0) {{
        return `<b>${{st.display_name}} (${{st.icao}})</b><br>No Polymarket weather markets.`;
      }}
      const sorted = st.markets.slice().sort((a, b) => {{
        const ad = a.target_date || "";
        const bd = b.target_date || "";
        return ad.localeCompare(bd);
      }});
      const items = sorted.slice(0, 10).map(mkt => {{
        const status = mkt.closed ? "closed" : (mkt.active ? "active" : "inactive");
        const date = mkt.target_date || "n/a";
        return `<li>${{date}} &middot; ${{status}} &middot; <a href="${{mkt.url}}" target="_blank" rel="noopener noreferrer">${{mkt.title}}</a></li>`;
      }}).join("");
      const more = sorted.length > 10 ? `<div>+ ${{sorted.length - 10}} more...</div>` : "";
      return (
        `<b>${{st.display_name}} (${{st.icao}})</b><br>` +
        `Polymarket weather markets: ${{st.markets.length}}<br>` +
        `<ul style="margin:4px 0 0 16px; padding:0;">${{items}}</ul>` +
        more
      );
    }}

    const stationsWithMarkets = DATA.stations.filter(st => st.markets && st.markets.length > 0);
    const mapStations = stationsWithMarkets.length > 0 ? stationsWithMarkets : DATA.stations;

    for (const st of mapStations) {{
      const marker = L.circleMarker([st.lat, st.lon], {{
        radius: 8,
        color: "#93c5fd",
        weight: 2,
        fillColor: "#3b82f6",
        fillOpacity: 0.8
      }})
        .addTo(map)
        .bindTooltip(`${{st.display_name}} (${{st.icao}}) — ${{st.markets.length}} market(s)`)
        .bindPopup(buildMarketsPopup(st));
      marker.on("click", () => {{
        stationSelect.value = st.slug;
        renderStation(st.slug);
      }});
      markers.push({{ slug: st.slug, marker }});
    }}

    if (mapStations.length > 0) {{
      const bounds = L.latLngBounds(mapStations.map(st => [st.lat, st.lon]));
      map.fitBounds(bounds, {{ padding: [40, 40], maxZoom: 6 }});
    }}

    function renderStation(slug) {{
      const st = stationMap.get(slug);
      if (!st) return;
      setActiveMarker(slug);
      stationMeta.textContent =
        `${{st.display_name}} (${{st.icao}}) | markets: ${{st.markets.length}} | obs rows: ${{st.observations.length}} | forecast rows: ${{st.forecasts.length}}`;

      const traces = [];

      const obsBySource = new Map();
      for (const row of st.observations) {{
        if (!obsBySource.has(row.source)) obsBySource.set(row.source, []);
        obsBySource.get(row.source).push(row);
      }}
      for (const [source, rows] of obsBySource.entries()) {{
        rows.sort((a, b) => a.date.localeCompare(b.date));
        traces.push({{
          x: rows.map(r => r.date),
          y: rows.map(r => r.value_f),
          mode: "lines+markers",
          name: `obs:${{source}}`,
          line: {{ width: 2 }},
          marker: {{ size: 6 }}
        }});
      }}

      const fcBySource = new Map();
      for (const row of st.forecasts) {{
        if (!fcBySource.has(row.source)) fcBySource.set(row.source, []);
        fcBySource.get(row.source).push(row);
      }}
      for (const [source, rows] of fcBySource.entries()) {{
        rows.sort((a, b) => a.target_date.localeCompare(b.target_date));
        traces.push({{
          x: rows.map(r => r.target_date),
          y: rows.map(r => r.predicted_max_f),
          mode: "lines+markers",
          name: `fc:${{source}}`,
          line: {{ width: 1.5, dash: "dot" }},
          marker: {{ size: 5, symbol: "diamond" }},
          customdata: rows.map(r => [r.run_time || "", r.predicted_std_f ?? ""]),
          hovertemplate:
            "date=%{{x}}<br>pred=%{{y}} F<br>run_time=%{{customdata[0]}}<br>std=%{{customdata[1]}}<extra></extra>"
        }});
      }}

      const layout = {{
        title: `${{st.display_name}} raw weather series`,
        template: "plotly_dark",
        paper_bgcolor: "#0b1020",
        plot_bgcolor: "#111933",
        margin: {{ l: 50, r: 20, t: 50, b: 40 }},
        xaxis: {{ title: "Date" }},
        yaxis: {{ title: "Temperature (F)" }},
        legend: {{ orientation: "h", y: -0.2 }}
      }};

      Plotly.newPlot("chart", traces, layout, {{ responsive: true, displaylogo: false }});
    }}

    stationSelect.addEventListener("change", (e) => renderStation(e.target.value));
    if (DATA.stations.length > 0) {{
      stationSelect.value = DATA.stations[0].slug;
      renderStation(DATA.stations[0].slug);
    }}
  </script>
</body>
</html>
"""
    # Template originally used doubled braces when this function returned an
    # f-string. We now inject payload via placeholder replacement, so collapse
    # doubled braces back to literal JS/CSS braces.
    return html.replace("__PAYLOAD__", payload).replace("{{", "{").replace("}}", "}")


def generate_dashboard_html(
    *,
    station_slugs: Iterable[str],
    lookback_days: int = 365,
    output_path: Path,
    all_forecast_runs: bool = False,
) -> dict:
    dataset = _compose_dataset(
        station_slugs,
        lookback_days=lookback_days,
        all_forecast_runs=all_forecast_runs,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_build_html(dataset), encoding="utf-8")
    return dataset

