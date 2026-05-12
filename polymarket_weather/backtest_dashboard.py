"""Static HTML dashboard for backtest JSON exports.

Feed the payload written by ``backtest_result_to_dict`` (CLI:
``cli.backtest --export-json ...``) into :func:`build_backtest_dashboard_html`
to get a self-contained file with Plotly charts and tables for evaluating
whether a backtest is honest and whether the strategy actually has edge.

Panels (top to bottom):

1. **Header KPIs** — model, strategy, window, fill counts, gross/net PnL, fees,
   max drawdown, realised log-loss.
2. **Equity curve** — cumulative gross PnL (already in the export) and a
   reconstructed cumulative *net*-of-fees curve, plus a drawdown ribbon shaded
   below.
3. **Reliability diagram** — for every settled fill the dashboard derives the
   probability the bet "claimed" (``p_model_at_post`` for a YES buy,
   ``1 - p_model_at_post`` for a YES sell), bins it into deciles, and plots
   mean predicted vs realised win frequency. The closer the dots track the
   diagonal, the better the model is calibrated *on the subset of buckets we
   actually traded* — which is the only calibration that matters for PnL.
4. **EV vs realised per-share PnL scatter** — ``expected_pnl_per_share_at_post``
   on the x-axis, realised per-share PnL (``realised_pnl_usd / shares`` minus
   the per-share fee on taker fills) on the y-axis. Each dot is one fill;
   coloured by side. A positive slope through the origin means the claimed
   edge survives in realised PnL.
5. **Station × Lead heatmap** — net PnL per (station, lead day) cell, coloured
   diverging red→green; cell text shows fill count.
6. **Tail vs centre hit rate** — for every event with a fill, the dashboard
   finds the bucket the model judged most likely (``argmax p_model_at_post``)
   and ranks every fill by absolute distance in bucket-index space. Wins are
   counted per distance bin. Where M1 leaks edge tends to show up here as a
   collapse in the far-tail win rate.
7. **Fill-rate / coverage panel** — taker fill rate (filled / edges found),
   maker fill rate (filled / posted), event-snapshot coverage with probs,
   and total bucket opportunities.
8. **Top winners / losers by event** — biggest realised-PnL events,
   sortable.
9. **Recent fills table** — enriched with realised label, won/lost colouring,
   and expected vs realised per-share columns.

The HTML is a single file; charts use the public Plotly CDN. No build step.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Backtest dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  :root {
    --bg: #0f172a;
    --panel: #1e293b;
    --panel-2: #111827;
    --border: #334155;
    --muted: #94a3b8;
    --text: #e2e8f0;
    --accent: #38bdf8;
    --green: #22c55e;
    --red: #ef4444;
    --amber: #f59e0b;
    --violet: #a78bfa;
  }
  body { margin: 0; font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); }
  .wrap { max-width: 1280px; margin: 0 auto; padding: 16px 20px 64px; }
  h1 { font-size: 1.35rem; margin: 0 0 4px; }
  h2 { font-size: 0.95rem; margin: 0 0 8px; color: #93c5fd; letter-spacing: 0.02em; }
  .meta { color: var(--muted); font-size: 0.85rem; margin-bottom: 20px; }
  .kpi { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 24px; }
  .kpi .tile { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; }
  .kpi .label { color: var(--muted); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; }
  .kpi .value { font-size: 1.2rem; font-weight: 600; margin-top: 2px; }
  .kpi .pos { color: var(--green); }
  .kpi .neg { color: var(--red); }
  .grid { display: grid; gap: 16px; grid-template-columns: 1fr; }
  @media (min-width: 980px) {
    .grid-2 { grid-template-columns: 1fr 1fr; }
  }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 14px; margin-bottom: 16px; }
  .plot { min-height: 320px; }
  .plot.tall { min-height: 380px; }
  .small { font-size: 0.82rem; color: var(--muted); margin-top: 8px; line-height: 1.4; }
  table { width: 100%; border-collapse: collapse; font-size: 0.84rem; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  th { color: var(--muted); font-weight: 500; text-transform: uppercase; font-size: 0.7rem; letter-spacing: 0.05em; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  tr.win td { color: #d1fae5; }
  tr.lose td { color: #fecaca; }
  tr.unsettled td { color: var(--muted); font-style: italic; }
  details summary { cursor: pointer; color: var(--accent); }
  .badge { display: inline-block; padding: 1px 6px; border-radius: 6px; font-size: 0.7rem; background: #0f172a; border: 1px solid var(--border); color: var(--muted); }
  .footer { color: var(--muted); font-size: 0.75rem; margin-top: 32px; text-align: center; }
</style>
</head>
<body>
<div class="wrap">
  <h1 id="title">Backtest dashboard</h1>
  <div class="meta" id="meta"></div>
  <div class="kpi" id="kpi"></div>

  <div class="card">
    <h2>Equity curve (gross, net, drawdown)</h2>
    <div id="equity" class="plot tall"></div>
    <div class="small">Gross is the cumulative settlement payoff per fill;
      net subtracts the per-fill <code>fee_usd</code> so the line you'd actually
      see in your account. The shaded area is running peak minus current
      cumulative PnL — bigger = bigger peak-to-trough drawdown to that point.</div>
  </div>

  <div class="grid grid-2">
    <div class="card">
      <h2>Reliability — predicted vs realised win rate (acted-on)</h2>
      <div id="reliability" class="plot"></div>
      <div class="small">For each settled fill we use <code>p_model_at_post</code> on
        the side we actually bet (so 1 − p for a sell). Bins are weighted by
        share count. Closer to the dotted diagonal = better calibrated <em>where
        it counted</em>. A line consistently below the diagonal at high
        predicted probabilities is overconfidence on "high-conviction" bets.</div>
    </div>
    <div class="card">
      <h2>Expected vs realised per-share PnL</h2>
      <div id="ev" class="plot"></div>
      <div class="small">x = <code>expected_pnl_per_share_at_post</code>
        (already net of the relevant fee), y = realised per-share PnL net of
        fee. A best-fit line with positive slope through the origin means the
        edge claims survive. A flat / negative slope means the alleged edge is
        noise, fees, or selection bias.</div>
    </div>
  </div>

  <div class="card">
    <h2>Net PnL heatmap — station × lead day (taker only)</h2>
    <div id="heatmap" class="plot tall"></div>
    <div class="small">Cells are net PnL (settled minus fees) summed across all
      taker fills posted at that lead. Cell labels show fill count. Useful for
      spotting where edge concentrates and where you mostly pay fees.</div>
  </div>

  <div class="grid grid-2">
    <div class="card">
      <h2>Tail vs centre hit rate</h2>
      <div id="tail" class="plot"></div>
      <div class="small">For every event with a fill we find the bucket the
        model judged most likely (argmax p_model). Each fill's distance from
        that bucket goes into a bin. The bar height is the win rate (taker buys
        win iff the realised label matches; sells win iff it doesn't). M1 tail
        overconfidence shows up as a collapse on the right side.</div>
    </div>
    <div class="card">
      <h2>Coverage and fill rate</h2>
      <div id="coverage"></div>
      <div class="small">Was edge missed because the model was silent, because
        the book never disagreed, or because caps blocked sized orders?</div>
    </div>
  </div>

  <div class="card">
    <h2>Top winners and losers by event</h2>
    <div style="display:grid;gap:16px;grid-template-columns:1fr;" id="topGrid">
      <div>
        <div class="badge" style="margin-bottom:6px;">winners</div>
        <div style="overflow-x:auto;"><table id="winnerTbl"><thead><tr>
          <th>Event</th><th>Station</th><th class="num">Net PnL</th><th class="num">Fills</th>
        </tr></thead><tbody></tbody></table></div>
      </div>
      <div>
        <div class="badge" style="margin-bottom:6px;">losers</div>
        <div style="overflow-x:auto;"><table id="loserTbl"><thead><tr>
          <th>Event</th><th>Station</th><th class="num">Net PnL</th><th class="num">Fills</th>
        </tr></thead><tbody></tbody></table></div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Recent fills (most recent 80)</h2>
    <div style="overflow-x:auto;"><table id="fillTbl"><thead><tr>
      <th>Filled</th><th>Station</th><th>Bucket</th><th>Side</th>
      <th class="num">Shares</th><th class="num">Price</th>
      <th class="num">p model</th><th class="num">Exp $/sh</th>
      <th>Realised</th><th class="num">Net PnL</th>
    </tr></thead><tbody></tbody></table></div>
  </div>

  <div class="footer">Generated by polymarket_weather.backtest_dashboard.</div>
</div>

<script>
const DATA = __PAYLOAD__;
const m  = DATA.meta || {};
const s  = DATA.summary || {};
const eq = (DATA.equity_curve || []);
const stStats = DATA.snapshot_stats || {};
const ftk = (DATA.fills_taker || []);
const fmk = (DATA.fills_maker || []);
const allFills = ftk.concat(fmk);

const PLOT_BG = "#0f172a";
const PANEL_BG = "#1e293b";
const fmt$ = (v) => (v == null) ? "—" : (v >= 0 ? "+$" : "-$") + Math.abs(v).toFixed(2);
const fmtPct = (v) => (v == null) ? "—" : (100 * v).toFixed(1) + "%";
const fmtNum = (v, d=2) => (v == null) ? "—" : Number(v).toFixed(d);

document.getElementById("title").textContent =
  "Backtest dashboard — " + (m.model_id || "?") + "  (" + (m.strategy || "?") + ")";
document.getElementById("meta").textContent =
  (m.start || "") + " .. " + (m.end || "") +
  "   schema v" + (m.export_version || 1) +
  "   snapshots=" + (s.n_snapshots || 0) +
  "   events_resolved=" + (s.n_events_resolved || 0) +
  "   subsample=1/" + (s.take_every_n_snapshots || 1);

// ---------- KPI tiles ----------------------------------------------------------
const kpiEl = document.getElementById("kpi");
const tile = (label, value, cls="") =>
  '<div class="tile"><div class="label">' + label + '</div>' +
  '<div class="value ' + cls + '">' + value + '</div></div>';
const grossPnl = (s.pnl_taker_usd || 0) + (s.pnl_maker_usd || 0);
const netPnl = s.net_pnl_usd != null ? s.net_pnl_usd : grossPnl - (s.fees_paid_usd || 0);
kpiEl.innerHTML =
  tile("Net PnL", fmt$(netPnl), netPnl >= 0 ? "pos" : "neg") +
  tile("Gross PnL", fmt$(grossPnl), grossPnl >= 0 ? "pos" : "neg") +
  tile("Fees paid", "$" + (s.fees_paid_usd || 0).toFixed(2)) +
  tile("Max DD", fmt$(s.max_drawdown_usd || 0), "neg") +
  tile("Taker fills", String(s.n_fills_taker || 0)) +
  tile("Maker fills", String(s.n_fills_maker || 0)) +
  tile("Log-loss", s.realised_log_loss == null ? "—" : s.realised_log_loss.toFixed(3)) +
  tile("Bankroll cap", "$" + ((m.caps && m.caps.bankroll_usd) || 0).toFixed(0));

// ---------- helpers ------------------------------------------------------------
const sideIsBuy = (side) => side === "taker_buy" || side === "maker_buy";
const sideIsTaker = (side) => side === "taker_buy" || side === "taker_sell";
function pActionFor(f) {
  // Probability the bet "claimed". For a YES buy that's p_model; for a YES
  // sell it's (1 - p_model). Equivalent for maker buys/sells.
  return sideIsBuy(f.side) ? f.p_model_at_post : 1 - f.p_model_at_post;
}
function realisedPerShareNet(f) {
  if (!f.settled || f.shares === 0) return null;
  const grossPerShare = f.realised_pnl_usd / f.shares;
  const feePerShare = f.shares > 0 ? (f.fee_usd || 0) / f.shares : 0;
  return grossPerShare - feePerShare;
}

// ---------- equity curve + drawdown -------------------------------------------
(function () {
  const xs = eq.map(r => r.filled_at);
  const grossY = eq.map(r => r.cumulative_pnl_usd);
  // Walk fills (sorted by filled_at) to build cumulative net & peak/drawdown.
  const fillsSorted = allFills
    .filter(f => f.settled)
    .slice()
    .sort((a, b) => a.filled_at.localeCompare(b.filled_at));
  let cumNet = 0, peak = 0;
  const netXs = [], netYs = [], ddYs = [];
  for (const f of fillsSorted) {
    cumNet += (f.realised_pnl_usd || 0) - (f.fee_usd || 0);
    peak = Math.max(peak, cumNet);
    netXs.push(f.filled_at);
    netYs.push(cumNet);
    ddYs.push(cumNet - peak);
  }
  const traces = [];
  if (xs.length) {
    traces.push({
      x: xs, y: grossY, mode: "lines", name: "gross",
      line: { color: "#38bdf8", width: 2 }
    });
  }
  if (netXs.length) {
    traces.push({
      x: netXs, y: netYs, mode: "lines", name: "net of fees",
      line: { color: "#22c55e", width: 2 }
    });
    traces.push({
      x: netXs, y: ddYs, mode: "lines", name: "drawdown",
      fill: "tozeroy", line: { color: "rgba(239,68,68,0.85)", width: 1 },
      yaxis: "y2"
    });
  }
  Plotly.newPlot("equity", traces, {
    template: "plotly_dark", paper_bgcolor: PANEL_BG, plot_bgcolor: PLOT_BG,
    margin: { l: 60, r: 60, t: 20, b: 50 },
    xaxis: { title: "Fill time (UTC)" },
    yaxis: { title: "Cumulative PnL (USD)", zeroline: true, zerolinecolor: "#475569" },
    yaxis2: { title: "Drawdown (USD)", overlaying: "y", side: "right", showgrid: false, zeroline: false },
    legend: { orientation: "h", y: -0.18 }
  }, { responsive: true, displaylogo: false });
})();

// ---------- reliability diagram -----------------------------------------------
(function () {
  const settled = allFills.filter(f => f.settled);
  if (!settled.length) {
    Plotly.newPlot("reliability", [], { template: "plotly_dark",
      annotations: [{ text: "no settled fills", showarrow: false }] });
    return;
  }
  const N_BINS = 10;
  const bins = Array.from({ length: N_BINS }, () => ({ wp: 0, w: 0 }));
  for (const f of settled) {
    const p = pActionFor(f);
    const claimedWin = sideIsBuy(f.side)
      ? (f.realised_label === f.bucket_label)
      : (f.realised_label !== f.bucket_label);
    const idx = Math.min(N_BINS - 1, Math.max(0, Math.floor(p * N_BINS)));
    const w = Math.max(1, f.shares || 1);
    bins[idx].w += w;
    bins[idx].wp += w * (claimedWin ? 1 : 0);
  }
  const xs = [], ys = [], sizes = [];
  for (let i = 0; i < N_BINS; i++) {
    if (bins[i].w === 0) continue;
    xs.push((i + 0.5) / N_BINS);
    ys.push(bins[i].wp / bins[i].w);
    sizes.push(8 + 16 * Math.sqrt(bins[i].w / settled.reduce((a,f)=>a+(f.shares||1), 0)));
  }
  const traces = [
    { x: [0,1], y: [0,1], mode: "lines", name: "ideal",
      line: { color: "#64748b", width: 1, dash: "dot" } },
    { x: xs, y: ys, mode: "lines+markers", name: "observed",
      line: { color: "#38bdf8" },
      marker: { size: sizes, color: "#38bdf8" } },
  ];
  Plotly.newPlot("reliability", traces, {
    template: "plotly_dark", paper_bgcolor: PANEL_BG, plot_bgcolor: PLOT_BG,
    margin: { l: 50, r: 20, t: 10, b: 50 },
    xaxis: { title: "predicted probability of action winning",
             range: [0, 1], dtick: 0.1 },
    yaxis: { title: "realised win rate (share-weighted)", range: [0, 1], dtick: 0.1 },
    showlegend: false
  }, { responsive: true, displaylogo: false });
})();

// ---------- EV vs realised per-share scatter ----------------------------------
(function () {
  const dots = allFills
    .filter(f => f.settled)
    .map(f => ({
      x: f.expected_pnl_per_share_at_post,
      y: realisedPerShareNet(f),
      side: f.side,
      shares: f.shares,
      label: f.station_slug + "/" + f.bucket_label
    }))
    .filter(d => d.x != null && d.y != null);
  if (!dots.length) {
    Plotly.newPlot("ev", [], { template: "plotly_dark",
      annotations: [{ text: "no settled fills", showarrow: false }] });
    return;
  }
  const groups = { taker_buy: [], taker_sell: [], maker_buy: [], maker_sell: [] };
  for (const d of dots) (groups[d.side] || groups["taker_buy"]).push(d);
  const colorMap = {
    taker_buy: "#38bdf8", taker_sell: "#0ea5e9",
    maker_buy: "#a78bfa", maker_sell: "#7c3aed"
  };
  const traces = [];
  for (const [side, arr] of Object.entries(groups)) {
    if (!arr.length) continue;
    traces.push({
      x: arr.map(d => d.x), y: arr.map(d => d.y), text: arr.map(d => d.label),
      mode: "markers", type: "scatter", name: side,
      marker: { color: colorMap[side], size: arr.map(d => 6 + Math.sqrt(d.shares)),
                line: { color: "#0f172a", width: 1 } },
      hovertemplate: "%{text}<br>EV %{x:.3f}<br>real %{y:.3f}<extra>"+side+"</extra>"
    });
  }
  // Best-fit line through the dots.
  const xs = dots.map(d => d.x), ys = dots.map(d => d.y);
  const mx = xs.reduce((a,b)=>a+b,0)/xs.length;
  const my = ys.reduce((a,b)=>a+b,0)/ys.length;
  let num = 0, den = 0;
  for (let i = 0; i < xs.length; i++) {
    num += (xs[i] - mx) * (ys[i] - my);
    den += (xs[i] - mx) * (xs[i] - mx);
  }
  const slope = den === 0 ? 0 : num / den;
  const intercept = my - slope * mx;
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  traces.push({
    x: [xMin, xMax],
    y: [slope * xMin + intercept, slope * xMax + intercept],
    mode: "lines", name: "fit (slope " + slope.toFixed(2) + ")",
    line: { color: "#f59e0b", width: 1.5, dash: "dash" }
  });
  // Reference identity line.
  traces.push({
    x: [xMin, xMax], y: [xMin, xMax], mode: "lines", name: "identity",
    line: { color: "#64748b", width: 1, dash: "dot" }
  });
  Plotly.newPlot("ev", traces, {
    template: "plotly_dark", paper_bgcolor: PANEL_BG, plot_bgcolor: PLOT_BG,
    margin: { l: 50, r: 20, t: 10, b: 50 },
    xaxis: { title: "expected per-share PnL at post (USD)" },
    yaxis: { title: "realised per-share PnL net of fee (USD)" },
    legend: { orientation: "h", y: -0.18 }
  }, { responsive: true, displaylogo: false });
})();

// ---------- station × lead heatmap (taker only) -------------------------------
(function () {
  if (!ftk.length) {
    Plotly.newPlot("heatmap", [], { template: "plotly_dark",
      annotations: [{ text: "no taker fills", showarrow: false }] });
    return;
  }
  const stations = Array.from(new Set(ftk.map(f => f.station_slug))).sort();
  const leads = Array.from(new Set(ftk.map(f => f.lead_days))).sort((a,b)=>a-b);
  const sumPnl = {}, count = {};
  for (const f of ftk) {
    const k = f.station_slug + "|" + f.lead_days;
    sumPnl[k] = (sumPnl[k] || 0) + (f.realised_pnl_usd - (f.fee_usd || 0));
    count[k] = (count[k] || 0) + 1;
  }
  const z = stations.map(st =>
    leads.map(ld => sumPnl[st + "|" + ld] != null ? sumPnl[st + "|" + ld] : null)
  );
  const text = stations.map(st =>
    leads.map(ld => {
      const c = count[st + "|" + ld] || 0;
      return c ? c.toString() : "";
    })
  );
  const absMax = Math.max(...Object.values(sumPnl).map(Math.abs), 1);
  Plotly.newPlot("heatmap", [{
    type: "heatmap",
    x: leads.map(String), y: stations,
    z: z, text: text, texttemplate: "%{text}",
    zmid: 0, zmin: -absMax, zmax: absMax,
    colorscale: [
      [0, "#7f1d1d"], [0.25, "#ef4444"], [0.5, "#1e293b"],
      [0.75, "#22c55e"], [1, "#14532d"]
    ],
    colorbar: { title: "USD" },
    hovertemplate: "%{y} lead %{x}d<br>PnL %{z:+.2f}<br>n=%{text}<extra></extra>"
  }], {
    template: "plotly_dark", paper_bgcolor: PANEL_BG, plot_bgcolor: PLOT_BG,
    margin: { l: 110, r: 30, t: 10, b: 50 },
    xaxis: { title: "Lead days at post" },
    yaxis: { title: "" }
  }, { responsive: true, displaylogo: false });
})();

// ---------- tail vs centre hit rate -------------------------------------------
(function () {
  // Group fills by event, pick argmax-p bucket per event from the fills'
  // own p_model_at_post values. We use the max-p bucket label as proxy for
  // the event's "central" bucket.
  const byEvent = {};
  for (const f of allFills) {
    if (!f.settled) continue;
    if (!byEvent[f.event_slug]) byEvent[f.event_slug] = [];
    byEvent[f.event_slug].push(f);
  }
  const dist_n = [0, 0, 0, 0]; // 0,1,2,3+ buckets from argmax
  const dist_w = [0, 0, 0, 0];
  for (const ev of Object.values(byEvent)) {
    // Map every fill to a bucket index by sorting by lo_f.
    const sorted = ev.slice()
      .filter(f => f.bucket_lo_f != null)
      .sort((a,b) => a.bucket_lo_f - b.bucket_lo_f);
    if (!sorted.length) continue;
    // Pick the argmax-p bucket among fills.
    let argmaxIdx = 0, bestP = -1;
    for (let i = 0; i < sorted.length; i++) {
      if (sorted[i].p_model_at_post > bestP) { bestP = sorted[i].p_model_at_post; argmaxIdx = i; }
    }
    for (let i = 0; i < sorted.length; i++) {
      const d = Math.abs(i - argmaxIdx);
      const bin = d >= 3 ? 3 : d;
      const f = sorted[i];
      const claimedWin = sideIsBuy(f.side)
        ? (f.realised_label === f.bucket_label)
        : (f.realised_label !== f.bucket_label);
      dist_n[bin] += 1;
      if (claimedWin) dist_w[bin] += 1;
    }
  }
  const labels = ["centre (0)", "near (1)", "far (2)", "far (3+)"];
  const winRate = dist_n.map((n, i) => n ? dist_w[i] / n : null);
  const counts = dist_n.map(n => n.toString());
  Plotly.newPlot("tail", [{
    type: "bar", x: labels, y: winRate.map(v => v == null ? 0 : v),
    text: counts.map((c, i) => "n=" + c),
    textposition: "outside",
    marker: {
      color: winRate.map(v => v == null ? "#475569" : (v > 0.5 ? "#22c55e" : (v > 0.3 ? "#f59e0b" : "#ef4444")))
    }
  }], {
    template: "plotly_dark", paper_bgcolor: PANEL_BG, plot_bgcolor: PLOT_BG,
    margin: { l: 50, r: 20, t: 20, b: 60 },
    xaxis: { title: "buckets from argmax-p (per event)" },
    yaxis: { title: "win rate", range: [0, 1] },
    showlegend: false
  }, { responsive: true, displaylogo: false });
})();

// ---------- coverage / fill-rate ----------------------------------------------
(function () {
  const tile = (label, value, hint="") =>
    '<div class="tile" style="margin-bottom:6px;">' +
    '<div class="label">' + label + '</div>' +
    '<div class="value">' + value + '</div>' +
    (hint ? '<div class="small" style="margin-top:0;">' + hint + '</div>' : '') +
    '</div>';
  const taker_fr = stStats.taker_fill_rate;
  const maker_fr = stStats.maker_fill_rate;
  const cov = stStats.n_event_snapshots
    ? (stStats.n_event_snapshots_with_probs / stStats.n_event_snapshots) : null;
  const html =
    tile("Taker fill rate", fmtPct(taker_fr),
         (stStats.n_taker_filled || 0) + " filled / " +
         (stStats.n_taker_edges_found || 0) + " edges found") +
    tile("Maker fill rate", fmtPct(maker_fr),
         (stStats.n_maker_orders_filled || 0) + " filled / " +
         (stStats.n_maker_orders_posted || 0) + " posted") +
    tile("Event-snapshots with probs", fmtPct(cov),
         (stStats.n_event_snapshots_with_probs || 0) + " of " +
         (stStats.n_event_snapshots || 0)) +
    tile("Bucket opportunities", String(stStats.n_bucket_opportunities || 0),
         "(snapshots × buckets evaluated)") +
    tile("Median snapshot spacing",
         stStats.median_hours_between != null ?
         stStats.median_hours_between.toFixed(2) + " h" : "—");
  document.getElementById("coverage").innerHTML =
    '<div class="kpi" style="margin-bottom:0;">' + html + '</div>';
})();

// ---------- top winners / losers ----------------------------------------------
(function () {
  const byEvent = {};
  for (const f of allFills) {
    const k = f.event_slug;
    if (!byEvent[k]) byEvent[k] = { event_slug: k, station: f.station_slug, pnl: 0, n: 0 };
    if (f.settled) {
      byEvent[k].pnl += (f.realised_pnl_usd || 0) - (f.fee_usd || 0);
      byEvent[k].n += 1;
    }
  }
  const arr = Object.values(byEvent).filter(r => r.n > 0);
  arr.sort((a,b) => b.pnl - a.pnl);
  const winners = arr.slice(0, 8);
  const losers = arr.slice().sort((a,b) => a.pnl - b.pnl).slice(0, 8);
  const fill = (id, rows) => {
    const tb = document.querySelector("#" + id + " tbody");
    tb.innerHTML = "";
    for (const r of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + r.event_slug + "</td>" +
        "<td>" + r.station + "</td>" +
        '<td class="num ' + (r.pnl >= 0 ? "win" : "lose") + '">' + fmt$(r.pnl) + "</td>" +
        '<td class="num">' + r.n + "</td>";
      tb.appendChild(tr);
    }
  };
  fill("winnerTbl", winners);
  fill("loserTbl", losers);
})();

// ---------- recent fills table ------------------------------------------------
(function () {
  const tb = document.querySelector("#fillTbl tbody");
  const rows = allFills.slice().sort((a,b) => b.filled_at.localeCompare(a.filled_at)).slice(0, 80);
  for (const f of rows) {
    const realPerNet = realisedPerShareNet(f);
    const cls = !f.settled ? "unsettled" : (f.realised_pnl_usd > 0 ? "win" : (f.realised_pnl_usd < 0 ? "lose" : ""));
    const tr = document.createElement("tr");
    tr.className = cls;
    tr.innerHTML =
      "<td>" + f.filled_at + "</td>" +
      "<td>" + f.station_slug + "</td>" +
      "<td>" + f.bucket_label + "</td>" +
      "<td>" + f.side + "</td>" +
      '<td class="num">' + f.shares + "</td>" +
      '<td class="num">' + Number(f.price).toFixed(3) + "</td>" +
      '<td class="num">' + Number(f.p_model_at_post).toFixed(3) + "</td>" +
      '<td class="num">' + (f.expected_pnl_per_share_at_post != null ? Number(f.expected_pnl_per_share_at_post).toFixed(3) : "—") + "</td>" +
      "<td>" + (f.realised_label || "—") + "</td>" +
      '<td class="num">' + (f.settled ? fmt$((f.realised_pnl_usd || 0) - (f.fee_usd || 0)) : "—") + "</td>";
    tb.appendChild(tr);
  }
})();
</script>
</body>
</html>
"""


def build_backtest_dashboard_html(data: dict[str, Any]) -> str:
    """Render a single-file Plotly dashboard from a backtest_result_to_dict bundle.

    The function is schema-tolerant: it works on v1 exports (no per-fill
    realised data) by simply dropping reliability / EV-vs-realised /
    heatmap / tail panels onto an "no settled fills" placeholder, which is
    rendered client-side.
    """
    payload = json.dumps(data, separators=(",", ":"))
    return _TEMPLATE.replace("__PAYLOAD__", payload)


def write_backtest_dashboard(
    data: dict[str, Any],
    *,
    output_path: Path,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(build_backtest_dashboard_html(data), encoding="utf-8")
    return output_path


# ---------------------------------------------------------------------------
# Multi-run compare dashboard
# ---------------------------------------------------------------------------


_COMPARE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Backtest compare</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  body { margin: 0; font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #e2e8f0; }
  .wrap { max-width: 1280px; margin: 0 auto; padding: 16px 20px 64px; }
  h1 { font-size: 1.35rem; margin: 0 0 4px; }
  h2 { font-size: 0.95rem; margin: 0 0 8px; color: #93c5fd; letter-spacing: 0.02em; }
  .meta { color: #94a3b8; font-size: 0.85rem; margin-bottom: 20px; }
  .card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 14px; margin-bottom: 16px; }
  .plot { min-height: 360px; }
  .small { font-size: 0.82rem; color: #94a3b8; margin-top: 8px; line-height: 1.4; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #334155; white-space: nowrap; }
  th { color: #94a3b8; font-weight: 500; text-transform: uppercase; font-size: 0.7rem; letter-spacing: 0.05em; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  td.win { color: #d1fae5; }
  td.lose { color: #fecaca; }
  .footer { color: #94a3b8; font-size: 0.75rem; margin-top: 32px; text-align: center; }
</style>
</head>
<body>
<div class="wrap">
  <h1 id="title">Backtest compare</h1>
  <div class="meta" id="meta"></div>

  <div class="card">
    <h2>Summary</h2>
    <div style="overflow-x:auto;"><table id="sumTbl"><thead><tr>
      <th>Run</th><th>Model</th><th>Strategy</th><th>Window</th>
      <th class="num">Snapshots</th><th class="num">Taker fills</th><th class="num">Maker fills</th>
      <th class="num">Net PnL</th><th class="num">Gross PnL</th><th class="num">Fees</th>
      <th class="num">Max DD</th><th class="num">Log-loss</th>
      <th class="num">Taker FR</th><th class="num">Maker FR</th>
    </tr></thead><tbody></tbody></table></div>
    <div class="small">Net PnL is gross settled minus per-fill fees. Max DD is computed
      on the gross fill-time path stored in <code>equity_curve</code>; recompute
      net DD from settled fills if you want fee-adjusted drawdowns.</div>
  </div>

  <div class="card">
    <h2>Net cumulative PnL — overlay</h2>
    <div id="equity" class="plot"></div>
    <div class="small">Each run's net-of-fees cumulative PnL on a shared time axis.
      If runs span different windows the curves still overlay honestly because
      both axes are absolute (USD vs UTC time).</div>
  </div>

  <div class="card">
    <h2>Reliability — overlay</h2>
    <div id="rel" class="plot"></div>
    <div class="small">Predicted win probability for the action (binned in
      deciles, share-weighted) vs realised win rate, per run. Closer to the
      diagonal = better calibration on the bets actually taken.</div>
  </div>

  <div class="card">
    <h2>Net PnL by station — grouped</h2>
    <div id="byStation" class="plot"></div>
    <div class="small">Per-station net PnL across all taker fills, one bar per run.</div>
  </div>

  <div class="card">
    <h2>Net PnL by lead day — grouped</h2>
    <div id="byLead" class="plot"></div>
    <div class="small">How edge concentrates by horizon. Negative bars at short
      lead days suggest adverse selection from late book moves; flat or
      negative bars at long lead suggest market-implied prob already prices
      the model's signal.</div>
  </div>

  <div class="footer">Generated by polymarket_weather.backtest_dashboard.</div>
</div>

<script>
const RUNS = __PAYLOAD__;
const PLOT_BG = "#0f172a";
const PANEL_BG = "#1e293b";
const PALETTE = ["#38bdf8", "#a78bfa", "#22c55e", "#f59e0b", "#ef4444", "#14b8a6", "#f472b6", "#facc15"];

document.getElementById("title").textContent = "Backtest compare — " + RUNS.length + " runs";
document.getElementById("meta").textContent = RUNS.map(r => r.label).join("   |   ");

const fmt$ = (v) => (v == null) ? "—" : (v >= 0 ? "+$" : "-$") + Math.abs(v).toFixed(2);
const fmtPct = (v) => (v == null) ? "—" : (100 * v).toFixed(1) + "%";

// ---------- summary table ----------------------------------------------------
(function () {
  const tb = document.querySelector("#sumTbl tbody");
  for (let i = 0; i < RUNS.length; i++) {
    const r = RUNS[i];
    const m = r.data.meta || {};
    const s = r.data.summary || {};
    const ss = r.data.snapshot_stats || {};
    const tr = document.createElement("tr");
    const netCls = (s.net_pnl_usd || 0) >= 0 ? "win" : "lose";
    tr.innerHTML =
      '<td><span style="display:inline-block;width:10px;height:10px;background:' + PALETTE[i % PALETTE.length] + ';border-radius:50%;margin-right:6px;"></span>' + r.label + '</td>' +
      "<td>" + (m.model_id || "—") + "</td>" +
      "<td>" + (m.strategy || "—") + "</td>" +
      "<td>" + (m.start || "?") + " .. " + (m.end || "?") + "</td>" +
      '<td class="num">' + (s.n_snapshots || 0) + "</td>" +
      '<td class="num">' + (s.n_fills_taker || 0) + "</td>" +
      '<td class="num">' + (s.n_fills_maker || 0) + "</td>" +
      '<td class="num ' + netCls + '">' + fmt$(s.net_pnl_usd) + "</td>" +
      '<td class="num">' + fmt$((s.pnl_taker_usd || 0) + (s.pnl_maker_usd || 0)) + "</td>" +
      '<td class="num">$' + (s.fees_paid_usd || 0).toFixed(2) + "</td>" +
      '<td class="num lose">' + fmt$(s.max_drawdown_usd) + "</td>" +
      '<td class="num">' + (s.realised_log_loss == null ? "—" : s.realised_log_loss.toFixed(3)) + "</td>" +
      '<td class="num">' + fmtPct(ss.taker_fill_rate) + "</td>" +
      '<td class="num">' + fmtPct(ss.maker_fill_rate) + "</td>";
    tb.appendChild(tr);
  }
})();

const sideIsBuy = (side) => side === "taker_buy" || side === "maker_buy";

function netEquityFor(run) {
  const ftk = run.data.fills_taker || [];
  const fmk = run.data.fills_maker || [];
  const settled = ftk.concat(fmk).filter(f => f.settled);
  settled.sort((a, b) => a.filled_at.localeCompare(b.filled_at));
  let cum = 0;
  const xs = [], ys = [];
  for (const f of settled) {
    cum += (f.realised_pnl_usd || 0) - (f.fee_usd || 0);
    xs.push(f.filled_at);
    ys.push(cum);
  }
  return { xs, ys };
}

// ---------- equity overlay ---------------------------------------------------
(function () {
  const traces = RUNS.map((r, i) => {
    const ne = netEquityFor(r);
    return {
      x: ne.xs, y: ne.ys, mode: "lines", name: r.label,
      line: { color: PALETTE[i % PALETTE.length], width: 2 }
    };
  });
  Plotly.newPlot("equity", traces, {
    template: "plotly_dark", paper_bgcolor: PANEL_BG, plot_bgcolor: PLOT_BG,
    margin: { l: 60, r: 20, t: 10, b: 50 },
    xaxis: { title: "Fill time (UTC)" },
    yaxis: { title: "Cumulative net PnL (USD)", zeroline: true, zerolinecolor: "#475569" },
    legend: { orientation: "h", y: -0.18 }
  }, { responsive: true, displaylogo: false });
})();

function reliabilityFor(run) {
  const fills = (run.data.fills_taker || []).concat(run.data.fills_maker || []).filter(f => f.settled);
  const N = 10;
  const bins = Array.from({ length: N }, () => ({ wp: 0, w: 0 }));
  for (const f of fills) {
    const p = sideIsBuy(f.side) ? f.p_model_at_post : 1 - f.p_model_at_post;
    const claimedWin = sideIsBuy(f.side)
      ? (f.realised_label === f.bucket_label)
      : (f.realised_label !== f.bucket_label);
    const idx = Math.min(N - 1, Math.max(0, Math.floor(p * N)));
    const w = Math.max(1, f.shares || 1);
    bins[idx].w += w;
    bins[idx].wp += w * (claimedWin ? 1 : 0);
  }
  const xs = [], ys = [];
  for (let i = 0; i < N; i++) {
    if (bins[i].w === 0) continue;
    xs.push((i + 0.5) / N);
    ys.push(bins[i].wp / bins[i].w);
  }
  return { xs, ys };
}

// ---------- reliability overlay ----------------------------------------------
(function () {
  const traces = [{
    x: [0,1], y: [0,1], mode: "lines", name: "ideal",
    line: { color: "#64748b", width: 1, dash: "dot" }, showlegend: true
  }];
  for (let i = 0; i < RUNS.length; i++) {
    const rel = reliabilityFor(RUNS[i]);
    if (!rel.xs.length) continue;
    traces.push({
      x: rel.xs, y: rel.ys, mode: "lines+markers", name: RUNS[i].label,
      line: { color: PALETTE[i % PALETTE.length] },
      marker: { color: PALETTE[i % PALETTE.length], size: 8 }
    });
  }
  Plotly.newPlot("rel", traces, {
    template: "plotly_dark", paper_bgcolor: PANEL_BG, plot_bgcolor: PLOT_BG,
    margin: { l: 50, r: 20, t: 10, b: 50 },
    xaxis: { title: "predicted prob of action winning", range: [0,1], dtick: 0.1 },
    yaxis: { title: "realised win rate", range: [0,1], dtick: 0.1 },
    legend: { orientation: "h", y: -0.18 }
  }, { responsive: true, displaylogo: false });
})();

// ---------- by-station / by-lead grouped bars --------------------------------
function netByKey(run, key) {
  const ftk = run.data.fills_taker || [];
  const out = {};
  for (const f of ftk) {
    const k = key === "station" ? f.station_slug : f.lead_days;
    out[k] = (out[k] || 0) + (f.realised_pnl_usd - (f.fee_usd || 0));
  }
  return out;
}

function groupedBars(targetId, keyKind, axisTitle) {
  const all = new Set();
  for (const r of RUNS) for (const k of Object.keys(netByKey(r, keyKind))) all.add(k);
  let categories = Array.from(all);
  if (keyKind === "lead") {
    categories = categories.map(Number).sort((a,b)=>a-b).map(String);
  } else {
    categories.sort();
  }
  const traces = RUNS.map((r, i) => {
    const m = netByKey(r, keyKind);
    return {
      type: "bar", name: r.label,
      x: categories,
      y: categories.map(k => m[k] != null ? m[k] : (m[Number(k)] != null ? m[Number(k)] : 0)),
      marker: { color: PALETTE[i % PALETTE.length] }
    };
  });
  Plotly.newPlot(targetId, traces, {
    template: "plotly_dark", paper_bgcolor: PANEL_BG, plot_bgcolor: PLOT_BG,
    margin: { l: 60, r: 20, t: 10, b: 60 },
    xaxis: { title: axisTitle, tickangle: keyKind === "station" ? -25 : 0 },
    yaxis: { title: "Net PnL (USD)", zeroline: true, zerolinecolor: "#475569" },
    barmode: "group",
    legend: { orientation: "h", y: -0.22 }
  }, { responsive: true, displaylogo: false });
}

groupedBars("byStation", "station", "Station");
groupedBars("byLead", "lead", "Lead day");
</script>
</body>
</html>
"""


def build_backtest_compare_html(
    runs: list[tuple[str, dict[str, Any]]],
) -> str:
    """Render an overlay dashboard for two or more backtest runs.

    ``runs`` is a list of ``(label, payload)`` tuples in the order they should
    appear in the legend. The HTML is fully self-contained.
    """
    encoded = [{"label": label, "data": data} for label, data in runs]
    payload = json.dumps(encoded, separators=(",", ":"))
    return _COMPARE_TEMPLATE.replace("__PAYLOAD__", payload)


def write_backtest_compare_dashboard(
    runs: list[tuple[str, dict[str, Any]]],
    *,
    output_path: Path,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(build_backtest_compare_html(runs), encoding="utf-8")
    return output_path
