"""Build the daily recommendations report.

Pure read-only. Writes a markdown report under ``reports/`` and returns a
structured payload that the orchestrator can also use to drive the
automation layer in later phases.

Pipeline per active event (target_date >= today, on a station we follow):

1. Load latest ``bucket_probs`` for the chosen model (default M2 with M1
   fallback). Project onto the simplex to enforce sum-to-1.
2. Load latest ``pm_market_snapshots`` per bucket (best_bid / best_ask / mid).
3. Derive a NO ``BookLevel`` from the YES book using neg-risk consistency
   (``no_bid ≈ 1 - yes_ask``, ``no_ask ≈ 1 - yes_bid``).
4. Run :mod:`negrisk.analyse_event` to flag risk-free arbs.
5. For each bucket, compute the best taker EV and maker quote pair.
6. Size each via fractional Kelly under tiny-bankroll caps.
7. Render markdown table per event + summary.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from . import config
from .db import station_id_by_slug, with_conn
from .models.baseline import MODEL_M1
from .models.m2_postprocessed_ensemble import MODEL_M2
from .strategy.edge import (
    BookLevel,
    BucketBook,
    FeeSchedule,
    best_taker_edge,
    maker_quote_yes_within_rewards,
)
from .strategy.negrisk import BucketView, analyse_event, coherent_model_probs
from .strategy.sizing import (
    CapsConfig,
    CapsState,
    SizedOrder,
    size_edge,
    tiny_bankroll_caps,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BucketRecommendation:
    bucket_label: str
    lo_f: float | None
    hi_f: float | None
    p_model: float
    market_yes_bid: float | None
    market_yes_ask: float | None
    market_yes_mid: float | None
    best_taker: SizedOrder | None
    maker_buy: SizedOrder | None
    maker_sell: SizedOrder | None


@dataclass(frozen=True)
class EventRecommendation:
    event_slug: str
    station_slug: str
    target_date: dt.date
    lead_days: int
    model_id: str
    model_run_time: dt.datetime | None
    sum_p_model: float
    sum_yes_bids: float
    sum_yes_asks: float
    arb_buy_all_yes: float
    arb_sell_all_yes: float
    flagged_arb: bool
    buckets: list[BucketRecommendation]
    total_taker_ev_usd: float
    total_taker_notional_usd: float


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _latest_bucket_probs(
    cur,
    *,
    model_id: str,
    station_slugs: list[str],
    today: dt.date,
    days_ahead: int,
) -> dict[tuple[str, str], tuple[float, dt.datetime]]:
    """Return ``{(event_slug, bucket_label): (prob, run_time)}`` for the
    latest run_time per (event, bucket) for the requested model."""
    horizon = today + dt.timedelta(days=days_ahead)
    cur.execute(
        """
        WITH latest AS (
            SELECT DISTINCT ON (bp.event_slug, bp.bucket_label)
                bp.event_slug, bp.bucket_label, bp.prob::float8 AS prob,
                bp.run_time
            FROM bucket_probs bp
            JOIN pm_events e ON e.event_slug = bp.event_slug
            JOIN stations  s ON s.station_id = e.station_id
            WHERE bp.model_id = %s
              AND e.target_date BETWEEN %s AND %s
              AND s.slug = ANY(%s)
            ORDER BY bp.event_slug, bp.bucket_label, bp.run_time DESC
        )
        SELECT event_slug, bucket_label, prob, run_time FROM latest
        """,
        (model_id, today, horizon, station_slugs),
    )
    return {
        (e, l): (float(p), rt) for e, l, p, rt in cur.fetchall()
    }


def _latest_snapshots(
    cur,
    event_slugs: list[str],
) -> dict[tuple[str, str], BookLevel]:
    if not event_slugs:
        return {}
    cur.execute(
        """
        SELECT DISTINCT ON (s.event_slug, s.bucket_label)
            s.event_slug,
            s.bucket_label,
            s.best_bid::float8,
            s.best_ask::float8,
            s.mid::float8,
            s.depth_jsonb
        FROM pm_market_snapshots s
        WHERE s.event_slug = ANY(%s)
        ORDER BY s.event_slug, s.bucket_label, s.snapshot_at DESC
        """,
        (event_slugs,),
    )
    out: dict[tuple[str, str], BookLevel] = {}
    for e, lab, bid, ask, mid, depth in cur.fetchall():
        bid_size = ask_size = 0.0
        if depth and isinstance(depth, dict):
            bids = depth.get("bids") or []
            asks = depth.get("asks") or []
            if bids:
                try:
                    bid_size = float(bids[0][1])
                except (TypeError, ValueError, IndexError):
                    pass
            if asks:
                try:
                    ask_size = float(asks[0][1])
                except (TypeError, ValueError, IndexError):
                    pass
        out[(e, lab)] = BookLevel(
            best_bid=None if bid is None else float(bid),
            best_ask=None if ask is None else float(ask),
            bid_size=bid_size,
            ask_size=ask_size,
            mid=None if mid is None else float(mid),
        )
    return out


def _active_events(
    cur,
    *,
    today: dt.date,
    days_ahead: int,
    station_slugs: list[str],
) -> list[tuple[str, str, dt.date, list[tuple[str, float | None, float | None]]]]:
    """Return ``[(event_slug, station_slug, target_date, [(label, lo, hi)...])]``."""
    horizon = today + dt.timedelta(days=days_ahead)
    cur.execute(
        """
        SELECT
            e.event_slug,
            s.slug                AS station_slug,
            e.target_date,
            b.bucket_label,
            b.lo_f::float8,
            b.hi_f::float8
        FROM pm_events e
        JOIN stations  s ON s.station_id = e.station_id
        JOIN pm_buckets b ON b.event_slug = e.event_slug
        WHERE e.target_date BETWEEN %s AND %s
          AND s.slug = ANY(%s)
        ORDER BY e.event_slug, b.bucket_label
        """,
        (today, horizon, station_slugs),
    )
    grouped: dict[str, dict] = {}
    for slug, station, target, label, lo, hi in cur.fetchall():
        ev = grouped.setdefault(
            slug,
            {"station": station, "target": target, "buckets": []},
        )
        ev["buckets"].append(
            (label, None if lo is None else float(lo), None if hi is None else float(hi))
        )
    return [
        (slug, ev["station"], ev["target"], ev["buckets"])
        for slug, ev in sorted(grouped.items())
    ]


# ---------------------------------------------------------------------------
# Per-event computation
# ---------------------------------------------------------------------------


def _no_book_from_yes(yes: BookLevel) -> BookLevel:
    """Neg-risk-consistent NO BookLevel inferred from YES."""
    bid = None if yes.best_ask is None else max(0.0, 1.0 - yes.best_ask)
    ask = None if yes.best_bid is None else min(1.0, 1.0 - yes.best_bid)
    mid = None
    if bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
    return BookLevel(
        best_bid=bid,
        best_ask=ask,
        bid_size=yes.ask_size,
        ask_size=yes.bid_size,
        mid=mid,
    )


def build_event_recommendation(
    *,
    event_slug: str,
    station_slug: str,
    target_date: dt.date,
    today: dt.date,
    buckets: list[tuple[str, float | None, float | None]],
    bucket_probs: dict[tuple[str, str], tuple[float, dt.datetime]],
    snapshots: dict[tuple[str, str], BookLevel],
    model_id: str,
    caps: CapsConfig,
    fees: FeeSchedule = FeeSchedule(),
    target_spread: float = 0.04,
    rewards_per_share: float = 0.0,
) -> EventRecommendation | None:
    raw_probs: dict[str, float] = {}
    run_time: dt.datetime | None = None
    for label, _, _ in buckets:
        rec = bucket_probs.get((event_slug, label))
        if rec is not None:
            raw_probs[label] = rec[0]
            run_time = rec[1] if run_time is None or rec[1] > run_time else run_time
    if not raw_probs:
        return None
    coherent = coherent_model_probs(raw_probs)

    views: list[BucketView] = []
    for label, _, _ in buckets:
        yes_book = snapshots.get((event_slug, label))
        views.append(
            BucketView(
                label=label,
                p_model=coherent.get(label, 0.0),
                yes_bid=yes_book.best_bid if yes_book else None,
                yes_ask=yes_book.best_ask if yes_book else None,
            )
        )
    analysis = analyse_event(views)

    state = CapsState()  # per-event state; portfolio caps tracked outside
    bucket_recs: list[BucketRecommendation] = []
    total_taker_ev = 0.0
    total_taker_notional = 0.0

    for label, lo, hi in buckets:
        p_yes = coherent.get(label, 0.0)
        yes_book = snapshots.get((event_slug, label)) or BookLevel(None, None)
        no_book = _no_book_from_yes(yes_book) if (
            yes_book.best_bid is not None or yes_book.best_ask is not None
        ) else None
        bb = BucketBook(yes=yes_book, no=no_book)

        # Best taker
        taker_edge = best_taker_edge(p_yes, bb, fees=fees)
        taker_order = (
            size_edge(taker_edge, caps=caps, state=state)
            if taker_edge is not None
            else None
        )
        if taker_order is not None:
            total_taker_ev += taker_order.expected_value_usd
            total_taker_notional += taker_order.notional_usd
            state = CapsState(
                used_per_bucket=state.used_per_bucket,
                used_per_event=state.used_per_event + taker_order.notional_usd,
                used_per_day=state.used_per_day,
                used_per_portfolio=state.used_per_portfolio,
            )

        # Maker pair
        buy_edge, sell_edge = maker_quote_yes_within_rewards(
            p_yes,
            yes_book,
            target_spread=target_spread,
            reward_per_share=rewards_per_share,
            fees=fees,
        )
        maker_buy_order = (
            size_edge(buy_edge, caps=caps, state=state)
            if buy_edge is not None and buy_edge.ev_per_share > 0
            else None
        )
        maker_sell_order = (
            size_edge(sell_edge, caps=caps, state=state)
            if sell_edge is not None and sell_edge.ev_per_share > 0
            else None
        )

        bucket_recs.append(
            BucketRecommendation(
                bucket_label=label,
                lo_f=lo,
                hi_f=hi,
                p_model=p_yes,
                market_yes_bid=yes_book.best_bid,
                market_yes_ask=yes_book.best_ask,
                market_yes_mid=yes_book.mid,
                best_taker=taker_order,
                maker_buy=maker_buy_order,
                maker_sell=maker_sell_order,
            )
        )

    lead_days = max(0, (target_date - today).days)
    return EventRecommendation(
        event_slug=event_slug,
        station_slug=station_slug,
        target_date=target_date,
        lead_days=lead_days,
        model_id=model_id,
        model_run_time=run_time,
        sum_p_model=analysis.sum_p_model,
        sum_yes_bids=analysis.sum_yes_bids,
        sum_yes_asks=analysis.sum_yes_asks,
        arb_buy_all_yes=analysis.arb_buy_all_yes,
        arb_sell_all_yes=analysis.arb_sell_all_yes,
        flagged_arb=analysis.flagged_arb,
        buckets=bucket_recs,
        total_taker_ev_usd=total_taker_ev,
        total_taker_notional_usd=total_taker_notional,
    )


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


def build_recommendations(
    *,
    station_slugs: list[str] | None = None,
    days_ahead: int = 7,
    primary_model: str = MODEL_M2,
    fallback_model: str = MODEL_M1,
    caps: CapsConfig | None = None,
    fees: FeeSchedule = FeeSchedule(),
    target_spread: float = 0.04,
) -> list[EventRecommendation]:
    today = dt.date.today()
    station_slugs = station_slugs or config.station_slugs()
    caps = caps or tiny_bankroll_caps()

    with with_conn() as conn, conn.cursor() as cur:
        events = _active_events(
            cur, today=today, days_ahead=days_ahead, station_slugs=station_slugs
        )
        if not events:
            return []
        event_slugs = [e[0] for e in events]
        primary_probs = _latest_bucket_probs(
            cur,
            model_id=primary_model,
            station_slugs=station_slugs,
            today=today,
            days_ahead=days_ahead,
        )
        fallback_probs = _latest_bucket_probs(
            cur,
            model_id=fallback_model,
            station_slugs=station_slugs,
            today=today,
            days_ahead=days_ahead,
        )
        snapshots = _latest_snapshots(cur, event_slugs)

    out: list[EventRecommendation] = []
    for slug, station, target, buckets in events:
        # Choose primary if it covers the event, else fall back.
        primary_count = sum(1 for label, _, _ in buckets if (slug, label) in primary_probs)
        chosen_probs = primary_probs
        chosen_id = primary_model
        if primary_count == 0:
            chosen_probs = fallback_probs
            chosen_id = fallback_model

        rec = build_event_recommendation(
            event_slug=slug,
            station_slug=station,
            target_date=target,
            today=today,
            buckets=buckets,
            bucket_probs=chosen_probs,
            snapshots=snapshots,
            model_id=chosen_id,
            caps=caps,
            fees=fees,
            target_spread=target_spread,
        )
        if rec is not None:
            out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _fmt_price(p: float | None) -> str:
    if p is None:
        return "—"
    return f"{p:.3f}"


def _fmt_int(p: float | None, default: str = "—") -> str:
    if p is None:
        return default
    return f"{p:.2f}"


def render_recommendations_markdown(
    recs: list[EventRecommendation],
    *,
    caps: CapsConfig,
    fees: FeeSchedule = FeeSchedule(),
    target_spread: float = 0.04,
) -> str:
    today = dt.date.today()
    lines: list[str] = []
    lines.append(f"# Recommendations — {today.isoformat()}")
    lines.append("")
    lines.append("**This is a read-only report. No orders are placed.**")
    lines.append("")
    lines.append("## Configuration")
    lines.append(
        f"- Bankroll: ${caps.bankroll_usd:.2f} | Kelly fraction: "
        f"{caps.kelly_fraction:.2f}"
    )
    lines.append(
        f"- Caps: per-bucket ${caps.per_bucket_usd:.2f}, "
        f"per-event ${caps.per_event_usd:.2f}, "
        f"per-day ${caps.per_day_usd:.2f}, "
        f"per-portfolio ${caps.per_portfolio_usd:.2f}"
    )
    lines.append(
        f"- Fees: taker {fees.taker_fee*100:.1f}% / maker "
        f"{fees.maker_fee*100:.1f}% / rewards spread "
        f"{fees.rewards_max_spread*100:.1f}c"
    )
    lines.append(
        f"- Min edge: {caps.min_edge_per_dollar*100:.1f}c per $1 of notional. "
        f"Maker target spread: {target_spread*100:.1f}c."
    )
    lines.append("")

    total_taker_ev = sum(r.total_taker_ev_usd for r in recs)
    total_notional = sum(r.total_taker_notional_usd for r in recs)
    lines.append(
        f"**Total taker EV** across all events: ${total_taker_ev:.2f} "
        f"on ${total_notional:.2f} notional"
    )
    lines.append("")

    # Event-level summary table
    lines.append("## Event summary")
    lines.append("")
    lines.append("| event | station | target | lead | model | sum p_model | sum bids | sum asks | arb_buy | arb_sell | taker_ev$ | taker_$ |")
    lines.append("|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in recs:
        lines.append(
            f"| {r.event_slug} | {r.station_slug} | {r.target_date} | "
            f"{r.lead_days} | {r.model_id} | "
            f"{r.sum_p_model:.3f} | {r.sum_yes_bids:.3f} | {r.sum_yes_asks:.3f} | "
            f"{r.arb_buy_all_yes:+.3f} | {r.arb_sell_all_yes:+.3f} | "
            f"{r.total_taker_ev_usd:+.2f} | {r.total_taker_notional_usd:.2f} |"
        )
    lines.append("")

    # Per-event detail
    for r in recs:
        if r.flagged_arb:
            arb_marker = " — **NEG-RISK ARB FLAGGED**"
        else:
            arb_marker = ""
        lines.append(f"## {r.event_slug}{arb_marker}")
        lines.append("")
        lines.append(
            f"_Station {r.station_slug}, target {r.target_date}, "
            f"lead {r.lead_days} days, model {r.model_id} "
            f"(run {r.model_run_time})._"
        )
        lines.append("")
        lines.append(
            "| bucket | p_model | yes_bid | yes_ask | yes_mid | "
            "taker action | taker price | taker shares | taker EV$ | "
            "maker buy | maker sell |"
        )
        lines.append("|---|---:|---:|---:|---:|---|---:|---:|---:|---|---|")
        for b in r.buckets:
            t = b.best_taker
            mb = b.maker_buy
            ms = b.maker_sell
            taker_action = "—" if t is None else t.edge.action.value
            taker_price = "—" if t is None else _fmt_price(t.edge.price)
            taker_shares = "—" if t is None else str(t.shares)
            taker_ev = "—" if t is None else f"{t.expected_value_usd:+.2f}"
            mbuy = (
                "—"
                if mb is None
                else f"{mb.shares}@{_fmt_price(mb.edge.price)} EV=${mb.expected_value_usd:+.2f}"
            )
            msell = (
                "—"
                if ms is None
                else f"{ms.shares}@{_fmt_price(ms.edge.price)} EV=${ms.expected_value_usd:+.2f}"
            )
            lines.append(
                f"| {b.bucket_label} | {b.p_model:.3f} | "
                f"{_fmt_price(b.market_yes_bid)} | {_fmt_price(b.market_yes_ask)} | "
                f"{_fmt_price(b.market_yes_mid)} | "
                f"{taker_action} | {taker_price} | {taker_shares} | {taker_ev} | "
                f"{mbuy} | {msell} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def write_recommendations_report(
    recs: list[EventRecommendation],
    *,
    caps: CapsConfig,
    fees: FeeSchedule = FeeSchedule(),
    target_spread: float = 0.04,
    out_path=None,
):
    paths = config.paths()
    paths.reports.mkdir(parents=True, exist_ok=True)
    if out_path is None:
        ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        out_path = paths.reports / f"recommendations_{ts}.md"
    md = render_recommendations_markdown(
        recs, caps=caps, fees=fees, target_spread=target_spread
    )
    out_path.write_text(md, encoding="utf-8")
    return out_path
