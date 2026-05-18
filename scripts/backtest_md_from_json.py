"""Render a backtest markdown report directly from an exported JSON bundle.

Useful when ``cli.backtest`` produced ``--export-json`` output but the
follow-up ``write_backtest_report`` step never finished (e.g. process hung
on DB teardown). All numbers come from the JSON, so this does not touch
Postgres.

Usage:
    python scripts/backtest_md_from_json.py reports/backtest_isotonic_dual_anchor.json
    python scripts/backtest_md_from_json.py path/to.json -o reports/custom.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path


def render_markdown(payload: dict) -> str:
    meta = payload.get("meta", {})
    summary = payload.get("summary", {})
    snapshot_stats = payload.get("snapshot_stats", {})
    by_station = payload.get("by_station", {})
    by_lead = payload.get("by_lead", {})
    notes = payload.get("notes", []) or []

    caps = meta.get("caps", {})
    fees = meta.get("fees", {})
    model_id = meta.get("model_id", "?")
    strategy = meta.get("strategy", "?")
    start = meta.get("start", "?")
    end = meta.get("end", "?")

    n_taker = summary.get("n_fills_taker", 0)
    n_maker = summary.get("n_fills_maker", 0)
    pnl_taker = summary.get("pnl_taker_usd", 0.0)
    pnl_maker = summary.get("pnl_maker_usd", 0.0)
    fees_paid = summary.get("fees_paid_usd", 0.0)
    net = summary.get("net_pnl_usd", pnl_taker + pnl_maker - fees_paid)
    max_dd = summary.get("max_drawdown_usd", 0.0)
    log_loss = summary.get("realised_log_loss")

    lines: list[str] = []
    lines.append(f"# Backtest \u2014 {model_id}")
    lines.append("")
    lines.append(f"- Window: {start} .. {end}")
    lines.append(f"- Strategy: {strategy}")
    lines.append(f"- Snapshots replayed: {summary.get('n_snapshots', 0)}")
    lines.append(f"- Events resolved: {summary.get('n_events_resolved', 0)}")
    cap_str = (
        f"per-bucket ${caps.get('per_bucket_usd', 0):.2f}, "
        f"per-event ${caps.get('per_event_usd', 0):.2f}, "
        f"per-day ${caps.get('per_day_usd', 0):.2f}, "
        f"bankroll ${caps.get('bankroll_usd', 0):.2f}, "
        f"kelly_fraction={caps.get('kelly_fraction', 0):.2f}"
    )
    if "min_edge_per_dollar" in caps:
        cap_str += f", min_edge_per_dollar={caps['min_edge_per_dollar']:.4f}"
    lines.append(f"- Caps: {cap_str}")
    if fees:
        lines.append(
            f"- Fees: taker={fees.get('taker_fee', 0):.4f}, "
            f"maker={fees.get('maker_fee', 0):.4f}"
        )
    lines.append("")

    lines.append("## PnL summary")
    lines.append("")
    lines.append(f"- Taker PnL: **${pnl_taker:+.2f}** (n_fills = {n_taker})")
    lines.append(f"- Maker PnL: **${pnl_maker:+.2f}** (n_fills = {n_maker})")
    lines.append(f"- Fees paid: ${fees_paid:.2f}")
    lines.append(
        f"- Net PnL (after taker fees): **${net:+.2f}** "
        f"(gross ${pnl_taker + pnl_maker:+.2f} \u2212 fees)"
    )
    if max_dd:
        lines.append(f"- Max drawdown on fee-adjusted equity path: **${max_dd:+.2f}**")
    if log_loss is not None:
        lines.append(f"- Realised log-loss on resolved events: {log_loss:.4f}")
    lines.append("")

    if snapshot_stats:
        lines.append("## Snapshot coverage")
        lines.append("")
        ss = snapshot_stats
        lines.append(f"- Snapshots used in replay: **{ss.get('n_snapshots', 0)}**")
        lines.append(
            f"- Total snapshot timestamps in DB window: "
            f"**{ss.get('total_snapshots_in_db', 0)}**"
        )
        if ss.get("median_hours_between") is not None:
            lines.append(
                f"- Median hours between consecutive used snapshots: "
                f"{ss['median_hours_between']:.2f} h"
            )
        if ss.get("mean_hours_between") is not None:
            lines.append(
                f"- Mean hours between consecutive used snapshots: "
                f"{ss['mean_hours_between']:.2f} h"
            )
        if ss.get("first_snapshot_at"):
            lines.append(f"- First snapshot at: {ss['first_snapshot_at']}")
        if ss.get("last_snapshot_at"):
            lines.append(f"- Last snapshot at:  {ss['last_snapshot_at']}")
        if (ss.get("take_every_n_snapshots") or 1) > 1:
            lines.append(
                f"- Subsample: every **{ss['take_every_n_snapshots']}** snapshot(s)"
            )
        if ss.get("min_snapshot_utc_hour") is not None:
            lines.append(
                f"- Min snapshot UTC hour: **{ss['min_snapshot_utc_hour']}** (filter)"
            )
        lines.append("")

    if notes:
        lines.append("## Notes")
        lines.append("")
        for n in notes:
            lines.append(f"- {n}")
        lines.append("")

    if by_station:
        lines.append("## By station (taker)")
        lines.append("")
        lines.append("| station | n | pnl |")
        lines.append("|---|---:|---:|")
        for st in sorted(by_station):
            v = by_station[st] or {}
            lines.append(f"| {st} | {v.get('n', 0)} | {v.get('pnl', 0):+.2f} |")
        lines.append("")

    if by_lead:
        lines.append("## By lead day (taker)")
        lines.append("")
        lines.append("| lead | n | pnl |")
        lines.append("|---:|---:|---:|")
        for lead in sorted(by_lead, key=lambda x: int(x)):
            v = by_lead[lead] or {}
            lines.append(f"| {lead} | {v.get('n', 0)} | {v.get('pnl', 0):+.2f} |")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        f"_Rendered by `scripts/backtest_md_from_json.py` at "
        f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}._"
    )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("json_path", type=Path, help="Path to exported backtest JSON.")
    p.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Where to write the markdown. Defaults to <json_path>.md.",
    )
    args = p.parse_args()

    payload = json.loads(args.json_path.read_text(encoding="utf-8"))
    md = render_markdown(payload)

    out = args.output or args.json_path.with_suffix(".md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"Wrote {out}  ({len(md):,} bytes)")


if __name__ == "__main__":
    main()
