"""Daily live + paper trading review report.

Read-only summarisation of:

* live ``orders`` (status, age, fill ratio, exposure)
* live ``fills`` (notional traded, fees paid)
* live ``daily_pnl`` aggregates
* the day's ``paper_trades`` for comparison
* the most recent ``calibration_runs`` row per model so we can spot drift
* unresolved events with open notional

Designed to be eyeballed every morning during Phase 6 to decide whether to
keep ``WEATHER_AUTOMATION_ENABLED=1`` set or trip ``WEATHER_KILL_SWITCH``.

The report is **never** allowed to call signed methods. It only reads from
Postgres.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .config import paths
from .db import with_conn

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class OrderSummary:
    order_id: str
    event_slug: str
    bucket_label: str
    target_date: dt.date
    side: str
    price: float
    requested_shares: int
    filled_shares: int
    status: str
    posted_at: dt.datetime
    p_model_at_post: float | None
    expected_value_usd: float | None


@dataclass
class FillSummary:
    fill_id: str
    order_id: str
    filled_at: dt.datetime
    side: str
    price: float
    shares: int
    fee_usd: float


@dataclass
class PaperSummaryRow:
    event_slug: str
    bucket_label: str
    target_date: dt.date
    side: str
    price: float
    shares: int
    expected_value_usd: float
    realised_pnl_usd: float | None
    realised_label: str | None


@dataclass
class CalibRow:
    run_id: str
    model_id: str
    ended_at: dt.datetime | None
    sample_n: int
    log_loss: float | None
    brier: float | None


@dataclass
class LiveReport:
    report_date: dt.date
    automation_enabled: bool
    kill_switch: bool
    daily_pnl: dict | None
    open_orders: list[OrderSummary] = field(default_factory=list)
    fills_today: list[FillSummary] = field(default_factory=list)
    paper_today: list[PaperSummaryRow] = field(default_factory=list)
    paper_settled_recent: list[PaperSummaryRow] = field(default_factory=list)
    calibrations: list[CalibRow] = field(default_factory=list)
    unresolved_events: list[tuple[str, int, float]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _fetch_open_orders() -> list[OrderSummary]:
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT order_id, event_slug, bucket_label, target_date, side,
                   price::float8, requested_shares, filled_shares, status,
                   posted_at, p_model_at_post::float8, expected_value_usd::float8
            FROM orders
            WHERE status IN ('open', 'partially_filled', 'unknown')
            ORDER BY posted_at DESC
            """
        )
        rows = cur.fetchall()
    return [
        OrderSummary(
            order_id=r[0], event_slug=r[1], bucket_label=r[2], target_date=r[3],
            side=r[4], price=float(r[5] or 0), requested_shares=int(r[6] or 0),
            filled_shares=int(r[7] or 0), status=r[8], posted_at=r[9],
            p_model_at_post=float(r[10]) if r[10] is not None else None,
            expected_value_usd=float(r[11]) if r[11] is not None else None,
        )
        for r in rows
    ]


def _fetch_fills_for_day(day: dt.date) -> list[FillSummary]:
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT fill_id, order_id, filled_at, side, price::float8,
                   shares, fee_usd::float8
            FROM fills
            WHERE DATE(filled_at AT TIME ZONE 'UTC') = %s
            ORDER BY filled_at DESC
            """,
            (day,),
        )
        rows = cur.fetchall()
    return [
        FillSummary(
            fill_id=r[0], order_id=r[1], filled_at=r[2], side=r[3],
            price=float(r[4] or 0), shares=int(r[5] or 0),
            fee_usd=float(r[6] or 0),
        )
        for r in rows
    ]


def _fetch_daily_pnl(day: dt.date) -> dict | None:
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT pnl_date, notional_traded_usd::float8, realised_pnl_usd::float8,
                   fees_paid_usd::float8, rewards_earned_usd::float8,
                   open_orders_count, fills_count, refreshed_at
            FROM daily_pnl
            WHERE pnl_date = %s
            """,
            (day,),
        )
        r = cur.fetchone()
    if r is None:
        return None
    return {
        "pnl_date": r[0],
        "notional_traded_usd": float(r[1] or 0),
        "realised_pnl_usd": float(r[2] or 0),
        "fees_paid_usd": float(r[3] or 0),
        "rewards_earned_usd": float(r[4] or 0),
        "open_orders_count": int(r[5] or 0),
        "fills_count": int(r[6] or 0),
        "refreshed_at": r[7],
    }


def _fetch_paper_for_day(day: dt.date) -> list[PaperSummaryRow]:
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_slug, bucket_label, target_date, side,
                   price::float8, shares, expected_value_usd::float8,
                   realised_pnl_usd::float8, realised_label
            FROM paper_trades
            WHERE DATE(posted_at AT TIME ZONE 'UTC') = %s
            ORDER BY posted_at DESC
            """,
            (day,),
        )
        rows = cur.fetchall()
    return [_paper_from_row(r) for r in rows]


def _fetch_paper_settled_recent(days: int) -> list[PaperSummaryRow]:
    cutoff = dt.date.today() - dt.timedelta(days=days)
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_slug, bucket_label, target_date, side,
                   price::float8, shares, expected_value_usd::float8,
                   realised_pnl_usd::float8, realised_label
            FROM paper_trades
            WHERE settled_at IS NOT NULL
              AND target_date >= %s
            ORDER BY settled_at DESC
            LIMIT 200
            """,
            (cutoff,),
        )
        rows = cur.fetchall()
    return [_paper_from_row(r) for r in rows]


def _paper_from_row(r) -> PaperSummaryRow:
    return PaperSummaryRow(
        event_slug=r[0], bucket_label=r[1], target_date=r[2], side=r[3],
        price=float(r[4] or 0), shares=int(r[5] or 0),
        expected_value_usd=float(r[6] or 0),
        realised_pnl_usd=float(r[7]) if r[7] is not None else None,
        realised_label=r[8],
    )


def _fetch_recent_calibrations() -> list[CalibRow]:
    """Latest calibration_runs row per model_id (using COALESCE on
    ended_at/started_at so in-flight rows still show up)."""
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (model_id)
                run_id, model_id, ended_at, sample_n,
                log_loss::float8, brier::float8
            FROM calibration_runs
            ORDER BY model_id, COALESCE(ended_at, started_at) DESC
            """
        )
        rows = cur.fetchall()
    return [
        CalibRow(
            run_id=str(r[0]), model_id=r[1], ended_at=r[2],
            sample_n=int(r[3] or 0),
            log_loss=float(r[4]) if r[4] is not None else None,
            brier=float(r[5]) if r[5] is not None else None,
        )
        for r in rows
    ]


def _fetch_unresolved_events_with_exposure() -> list[tuple[str, int, float]]:
    """Events that we have open exposure on but no realised observation yet."""
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.event_slug,
                   COUNT(*) AS n_open,
                   COALESCE(SUM(o.price::float8 * (o.requested_shares - o.filled_shares - o.cancelled_shares)), 0) AS open_notional
            FROM orders o
            WHERE o.status IN ('open', 'partially_filled', 'unknown')
            GROUP BY o.event_slug
            ORDER BY open_notional DESC
            """
        )
        rows = cur.fetchall()
    return [(r[0], int(r[1]), float(r[2] or 0)) for r in rows]


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_live_report(
    *,
    report_date: dt.date | None = None,
    paper_lookback_days: int = 14,
) -> LiveReport:
    from .automation.order_manager import is_automation_enabled, is_kill_switch_set

    day = report_date or dt.date.today()
    return LiveReport(
        report_date=day,
        automation_enabled=is_automation_enabled(),
        kill_switch=is_kill_switch_set(),
        daily_pnl=_fetch_daily_pnl(day),
        open_orders=_fetch_open_orders(),
        fills_today=_fetch_fills_for_day(day),
        paper_today=_fetch_paper_for_day(day),
        paper_settled_recent=_fetch_paper_settled_recent(paper_lookback_days),
        calibrations=_fetch_recent_calibrations(),
        unresolved_events=_fetch_unresolved_events_with_exposure(),
    )


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _fmt_money(x: float | None) -> str:
    if x is None:
        return "—"
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}"


def _fmt_price(x: float | None) -> str:
    return f"{x:.3f}" if x is not None else "—"


def _fmt_pct(x: float | None) -> str:
    return f"{100*x:5.2f}%" if x is not None else "—"


def render_live_report_markdown(report: LiveReport) -> str:
    lines: list[str] = []
    lines.append(f"# Live + paper trading review — {report.report_date.isoformat()}")
    lines.append("")
    if report.automation_enabled and not report.kill_switch:
        lines.append(
            "**Automation: LIVE.** `WEATHER_AUTOMATION_ENABLED=1`, kill switch unset."
        )
    elif report.automation_enabled and report.kill_switch:
        lines.append(
            "**Automation: LIVE-but-HALTED.** `WEATHER_KILL_SWITCH` is set; placement disabled, cancel-only."
        )
    else:
        lines.append("**Automation: paper-only.** `WEATHER_AUTOMATION_ENABLED` is not set.")
    lines.append("")

    # Daily PnL
    lines.append("## Daily PnL summary")
    lines.append("")
    p = report.daily_pnl
    if p is None:
        lines.append("_No `daily_pnl` row yet for today — has the orchestrator ticked?_")
    else:
        lines.append("| metric | value |")
        lines.append("|---|---|")
        lines.append(f"| notional traded | {_fmt_money(p['notional_traded_usd'])} |")
        lines.append(f"| realised PnL | {_fmt_money(p['realised_pnl_usd'])} |")
        lines.append(f"| fees paid | {_fmt_money(p['fees_paid_usd'])} |")
        lines.append(f"| rewards earned | {_fmt_money(p['rewards_earned_usd'])} |")
        lines.append(f"| open orders | {p['open_orders_count']} |")
        lines.append(f"| fills | {p['fills_count']} |")
        lines.append(f"| refreshed at | {p['refreshed_at']} |")
    lines.append("")

    # Open orders
    lines.append(f"## Open / unknown orders ({len(report.open_orders)})")
    lines.append("")
    if not report.open_orders:
        lines.append("_None._")
    else:
        lines.append(
            "| event | bucket | side | price | req | fill | status | p_model | EV | posted_at |"
        )
        lines.append("|---|---|---|---:|---:|---:|---|---:|---:|---|")
        for o in report.open_orders[:50]:
            lines.append(
                "| {ev} | {b} | {side} | {price} | {req} | {fill} | {status} "
                "| {pm} | {ev_usd} | {posted} |".format(
                    ev=o.event_slug, b=o.bucket_label, side=o.side,
                    price=_fmt_price(o.price), req=o.requested_shares,
                    fill=o.filled_shares, status=o.status,
                    pm=_fmt_pct(o.p_model_at_post),
                    ev_usd=_fmt_money(o.expected_value_usd),
                    posted=o.posted_at.strftime("%Y-%m-%d %H:%M"),
                )
            )
        if len(report.open_orders) > 50:
            lines.append(f"_… {len(report.open_orders) - 50} more rows omitted._")
    lines.append("")

    # Fills today
    lines.append(f"## Fills today ({len(report.fills_today)})")
    lines.append("")
    if not report.fills_today:
        lines.append("_None._")
    else:
        lines.append("| filled_at | order_id | side | price | shares | fee |")
        lines.append("|---|---|---|---:|---:|---:|")
        for f in report.fills_today[:50]:
            lines.append(
                "| {ts} | {oid} | {side} | {price} | {shares} | {fee} |".format(
                    ts=f.filled_at.strftime("%H:%M:%S"),
                    oid=f.order_id[:18],
                    side=f.side,
                    price=_fmt_price(f.price),
                    shares=f.shares,
                    fee=_fmt_money(f.fee_usd),
                )
            )
    lines.append("")

    # Paper trades today
    lines.append(f"## Paper trades posted today ({len(report.paper_today)})")
    lines.append("")
    if not report.paper_today:
        lines.append("_None._")
    else:
        gross_ev = sum(p.expected_value_usd for p in report.paper_today)
        lines.append(
            f"_Gross expected value across today's paper trades: {_fmt_money(gross_ev)}_"
        )
    lines.append("")

    # Paper settled recently
    lines.append(
        f"## Paper trades settled in last {len(report.paper_settled_recent)} rows"
    )
    lines.append("")
    if report.paper_settled_recent:
        realised = [
            p.realised_pnl_usd for p in report.paper_settled_recent
            if p.realised_pnl_usd is not None
        ]
        ev = [p.expected_value_usd for p in report.paper_settled_recent]
        if realised:
            n = len(realised)
            avg_real = sum(realised) / n
            avg_ev = sum(ev) / len(ev) if ev else 0
            lines.append(
                f"_n={n}, sum realised PnL={_fmt_money(sum(realised))}, "
                f"avg realised={_fmt_money(avg_real)}, avg EV={_fmt_money(avg_ev)}._"
            )
            if avg_ev != 0:
                ratio = avg_real / avg_ev
                lines.append(
                    f"_realised / expected ratio: {ratio:+.2f} "
                    "(close to 1.0 means model EV is honest, < 0 means edge is illusory)._"
                )
    else:
        lines.append("_None settled yet in lookback window._")
    lines.append("")

    # Calibration drift
    lines.append("## Latest calibration per model")
    lines.append("")
    if not report.calibrations:
        lines.append(
            "_No calibration_runs rows; run "
            "`python -m polymarket_weather.cli.calibrate --include-market`._"
        )
    else:
        lines.append("| model | sample_n | log_loss | brier | ended_at |")
        lines.append("|---|---:|---:|---:|---|")
        for c in report.calibrations:
            ts = c.ended_at.strftime("%Y-%m-%d %H:%M") if c.ended_at else "—"
            lines.append(
                "| {m} | {n} | {ll} | {br} | {ts} |".format(
                    m=c.model_id, n=c.sample_n,
                    ll=f"{c.log_loss:.4f}" if c.log_loss is not None else "—",
                    br=f"{c.brier:.4f}" if c.brier is not None else "—",
                    ts=ts,
                )
            )
    lines.append("")

    # Unresolved events with exposure
    lines.append(
        f"## Open exposure by event ({len(report.unresolved_events)})"
    )
    lines.append("")
    if not report.unresolved_events:
        lines.append("_None._")
    else:
        lines.append("| event | open orders | open notional |")
        lines.append("|---|---:|---:|")
        for slug, n, notional in report.unresolved_events[:30]:
            lines.append(f"| {slug} | {n} | {_fmt_money(notional)} |")
    lines.append("")

    # Manual review checklist
    lines.append("## Pre-trade morning review checklist")
    lines.append("")
    lines.append(
        "1. Confirm `daily_pnl.notional_traded_usd` ≤ per-day cap from `run_loop.py`.\n"
        "2. Confirm there are no `status='unknown'` rows older than 30 minutes; investigate any.\n"
        "3. Confirm latest M2 calibration log-loss is within ~0.02 nats of the M2 backtest claim. Drift is the first sign of regime change.\n"
        "4. Confirm at least one of M0/M1/M2 still beats the `market:mid` baseline on resolved events; if none, set `WEATHER_KILL_SWITCH=1` and investigate before scaling.\n"
        "5. Confirm `paper_trades` realised / expected ratio (above) is ≥ 0.5 over the last 30 days before scaling caps.\n"
        "6. Confirm Polymarket fees and reward parameters in `pm_market_snapshots.raw->'feeSchedule'` haven't changed since you last calibrated.\n"
        "7. Spot-check today's largest open order: does the model probability still look sane vs the current book mid?"
    )
    lines.append("")

    return "\n".join(lines)


def write_live_report(
    report: LiveReport, *, report_dir: Path | None = None
) -> Path:
    out_dir = report_dir or (paths().repo_root / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = out_dir / f"live_{report.report_date.isoformat()}.md"
    fname.write_text(render_live_report_markdown(report), encoding="utf-8")
    return fname
