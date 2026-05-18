"""Render a backtest markdown report directly from an exported JSON.

Useful when the producing CLI was interrupted before ``write_backtest_report``
ran, but the JSON bundle is intact on disk.
"""

import argparse
import json
from pathlib import Path


def render(d: dict) -> str:
    m = d.get("meta", {})
    s = d.get("summary", {})
    ss = d.get("snapshot_stats", {})
    net = s.get("pnl_taker_usd", 0.0) + s.get("pnl_maker_usd", 0.0) - s.get("fees_paid_usd", 0.0)
    caps = m.get("caps", {})

    out: list[str] = []
    out.append(f"# Backtest — {m.get('model_id', 'unknown')} (isotonic-recalibrated bucket_probs)")
    out.append("")
    out.append(f"- Window: {m.get('start')} .. {m.get('end')}")
    out.append(f"- Strategy: {m.get('strategy')}")
    out.append(f"- Snapshots replayed: {s.get('n_snapshots', 0)}")
    out.append(f"- Events resolved: {s.get('n_events_resolved', 0)}")
    out.append(
        f"- Caps: per-bucket ${caps.get('per_bucket_usd', 0):.2f}, "
        f"per-event ${caps.get('per_event_usd', 0):.2f}, "
        f"kelly_fraction={caps.get('kelly_fraction', 0):.2f}"
    )
    out.append("")
    out.append("## PnL summary")
    out.append("")
    out.append(
        f"- Taker PnL: **${s.get('pnl_taker_usd', 0):+.2f}** "
        f"(n_fills = {s.get('n_fills_taker', 0)})"
    )
    out.append(
        f"- Maker PnL: **${s.get('pnl_maker_usd', 0):+.2f}** "
        f"(n_fills = {s.get('n_fills_maker', 0)})"
    )
    out.append(f"- Fees paid: ${s.get('fees_paid_usd', 0):.2f}")
    out.append(f"- Net PnL (after taker fees): **${net:+.2f}**")
    out.append(
        f"- Max drawdown on fee-adjusted equity path: "
        f"**${s.get('max_drawdown_usd', 0):+.2f}**"
    )
    if s.get("realised_log_loss") is not None:
        out.append(f"- Realised log-loss on resolved events: {s['realised_log_loss']:.4f}")
    out.append("")
    out.append("## Snapshot coverage")
    out.append("")
    out.append(f"- Snapshots used in replay: **{ss.get('n_snapshots', 0)}**")
    out.append(
        f"- Total snapshot timestamps in DB window: "
        f"**{ss.get('total_snapshots_in_db', 0)}**"
    )
    if ss.get("median_hours_between") is not None:
        out.append(
            f"- Median hours between consecutive used snapshots: "
            f"{ss['median_hours_between']:.2f} h"
        )
    if (ss.get("take_every_n_snapshots") or 1) > 1:
        out.append(
            f"- Subsample: every **{ss['take_every_n_snapshots']}** snapshot(s)"
        )
    if ss.get("min_snapshot_utc_hour") is not None:
        out.append(
            f"- Min snapshot UTC hour: **{ss['min_snapshot_utc_hour']}** (filter)"
        )

    if d.get("notes"):
        out.append("")
        out.append("## Notes")
        out.append("")
        for n in d["notes"]:
            out.append(f"- {n}")

    if d.get("by_station"):
        out.append("")
        out.append("## By station (taker)")
        out.append("")
        out.append("| station | n | pnl |")
        out.append("|---|---:|---:|")
        for st in sorted(d["by_station"]):
            v = d["by_station"][st]
            out.append(f"| {st} | {v['n']} | {v['pnl']:+.2f} |")

    if d.get("by_lead"):
        out.append("")
        out.append("## By lead day (taker)")
        out.append("")
        out.append("| lead | n | pnl |")
        out.append("|---:|---:|---:|")
        for lead in sorted(d["by_lead"]):
            v = d["by_lead"][lead]
            out.append(f"| {lead} | {v['n']} | {v['pnl']:+.2f} |")

    out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("json_path", type=Path, help="Path to backtest JSON export.")
    ap.add_argument(
        "-o", "--out", type=Path, default=None,
        help="Output markdown path (default: <json>.md).",
    )
    args = ap.parse_args()
    payload = json.loads(args.json_path.read_text(encoding="utf-8"))
    out = args.out or args.json_path.with_suffix(".md")
    out.write_text(render(payload), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
