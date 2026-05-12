"""Static HTML dashboard for a single backtest JSON export.

Feed :func:`backtest_result_to_dict` output (written by ``cli.backtest
--export-json``) into :func:`build_backtest_dashboard_html` to get a self-contained
file with Plotly charts: equity curve, PnL by station / lead, and fill tables.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def build_backtest_dashboard_html(data: dict[str, Any]) -> str:
    payload = json.dumps(data, separators=(",", ":"))
    html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Backtest dashboard</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    body { margin: 0; font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0; }
    .wrap { max-width: 1200px; margin: 0 auto; padding: 16px; }
    h1 { font-size: 1.25rem; margin: 0 0 8px; }
    .meta { color: #94a3b8; font-size: 0.9rem; margin-bottom: 20px; }
    .grid { display: grid; gap: 16px; grid-template-columns: 1fr; }
    @media (min-width: 900px) {
      .grid2 { grid-template-columns: 1fr 1fr; }
    }
    .card { background: #1e293b; border-radius: 10px; padding: 12px; border: 1px solid #334155; }
    .card h2 { font-size: 1rem; margin: 0 0 8px; color: #93c5fd; }
    table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
    th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #334155; }
    th { color: #94a3b8; }
    .plot { min-height: 320px; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1 id="title">Backtest dashboard</h1>
    <div class="meta" id="meta"></div>
    <div class="grid">
      <div class="card"><h2>Cumulative PnL (gross, by fill time)</h2><div id="equity" class="plot"></div></div>
      <div class="grid grid2">
        <div class="card"><h2>Taker PnL by station</h2><div id="bySt" class="plot"></div></div>
        <div class="card"><h2>Taker PnL by lead day</h2><div id="byLead" class="plot"></div></div>
      </div>
      <div class="card"><h2>Snapshot coverage</h2><pre id="snap" style="margin:0;white-space:pre-wrap;font-size:0.85rem;"></pre></div>
      <div class="card"><h2>Recent taker fills</h2><div style="overflow-x:auto;"><table id="tbl"><thead><tr>
        <th>Time</th><th>Station</th><th>Bucket</th><th>Side</th><th>Shares</th><th>Price</th>
      </tr></thead><tbody></tbody></table></div></div>
    </div>
  </div>
  <script>
    const DATA = __PAYLOAD__;
    const m = DATA.meta || {};
    const s = DATA.summary || {};
    document.getElementById("title").textContent =
      `Backtest — ${m.model_id || "?"} (${m.strategy || "?"})`;
    document.getElementById("meta").textContent =
      `${m.start || ""} .. ${m.end || ""} | snapshots=${s.n_snapshots} | ` +
      `taker fills=${s.n_fills_taker} | maker fills=${s.n_fills_maker} | ` +
      `net PnL $${(s.net_pnl_usd ?? 0).toFixed(2)} | max DD $${(s.max_drawdown_usd ?? 0).toFixed(2)}`;

    const eq = DATA.equity_curve || [];
    if (eq.length) {
      Plotly.newPlot("equity", [{
        x: eq.map(r => r.filled_at),
        y: eq.map(r => r.cumulative_pnl_usd),
        mode: "lines",
        name: "cumulative",
        line: { color: "#38bdf8", width: 2 }
      }], {
        template: "plotly_dark",
        paper_bgcolor: "#1e293b",
        plot_bgcolor: "#0f172a",
        margin: { l: 50, r: 20, t: 30, b: 50 },
        xaxis: { title: "Fill time (UTC)" },
        yaxis: { title: "USD" },
        showlegend: false
      }, { responsive: true, displaylogo: false });
    }

    const bySt = DATA.by_station || {};
    const stKeys = Object.keys(bySt).sort();
    if (stKeys.length) {
      Plotly.newPlot("bySt", [{
        type: "bar",
        x: stKeys,
        y: stKeys.map(k => bySt[k].pnl || 0),
        marker: { color: "#a78bfa" }
      }], {
        template: "plotly_dark",
        paper_bgcolor: "#1e293b",
        plot_bgcolor: "#0f172a",
        margin: { l: 50, r: 20, t: 20, b: 80 },
        xaxis: { title: "Station", tickangle: -35 },
        yaxis: { title: "Taker PnL USD" },
        showlegend: false
      }, { responsive: true, displaylogo: false });
    }

    const byLead = DATA.by_lead || {};
    const leadKeys = Object.keys(byLead).map(Number).sort((a,b)=>a-b);
    if (leadKeys.length) {
      Plotly.newPlot("byLead", [{
        type: "bar",
        x: leadKeys.map(String),
        y: leadKeys.map(k => byLead[k].pnl || 0),
        marker: { color: "#34d399" }
      }], {
        template: "plotly_dark",
        paper_bgcolor: "#1e293b",
        plot_bgcolor: "#0f172a",
        margin: { l: 50, r: 20, t: 20, b: 50 },
        xaxis: { title: "Lead day" },
        yaxis: { title: "Taker PnL USD" },
        showlegend: false
      }, { responsive: true, displaylogo: false });
    }

    document.getElementById("snap").textContent = JSON.stringify(DATA.snapshot_stats || {}, null, 2);

    const fills = (DATA.fills_taker || []).slice(-80).reverse();
    const tb = document.querySelector("#tbl tbody");
    for (const f of fills) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${f.filled_at}</td><td>${f.station_slug}</td><td>${f.bucket_label}</td>` +
        `<td>${f.side}</td><td>${f.shares}</td><td>${f.price.toFixed(3)}</td>`;
      tb.appendChild(tr);
    }
  </script>
</body>
</html>
"""
    return html.replace("__PAYLOAD__", payload)


def write_backtest_dashboard(
    data: dict[str, Any],
    *,
    output_path: Path,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(build_backtest_dashboard_html(data), encoding="utf-8")
    return output_path
