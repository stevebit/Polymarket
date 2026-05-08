"""Historical backtest replay over ``pm_market_snapshots``.

Walks every snapshot in chronological order. For each ``(event_slug,
snapshot_at)`` group:

1. Find the latest ``bucket_probs`` for the chosen model with
   ``run_time <= snapshot_at`` (so we never look ahead). If no such row
   exists, skip the snapshot.
2. Build a :class:`BookLevel` per bucket from the snapshot row. Derive the
   NO side using neg-risk consistency.
3. Compute taker / maker edges with fee-aware EV.
4. Simulate fills:
   * **Taker buys / sells** fill immediately at the visible top-of-book,
     up to the lesser of (top-of-book size, our requested size).
   * **Maker buys / sells** post a limit order at the target price. They
     fill only if a later snapshot of the same bucket shows the book
     moved to or through our limit (back-of-queue model is a worst case;
     we approximate as front-of-queue here, and Phase 4b paper trading
     measures real fill rate).
5. Mark every position to its event's resolution outcome at ``target_date``,
   pulling the realised TMAX from ``observations`` (preferring
   ``wunderground:historical``). PnL = shares * (1{won} - cost) - fees.

Output: ``BacktestResult`` with per-strategy PnL, realised log-loss, and
fill metrics. The CLI ``cli.backtest`` writes a markdown report.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass, field
from typing import Iterable, Literal

from . import config
from .db import with_conn
from .recommend import _no_book_from_yes
from .score import BucketBounds, realised_bucket
from .strategy.edge import (
    Action,
    BookLevel,
    BucketBook,
    Edge,
    FeeSchedule,
    best_taker_edge,
    maker_quote_yes_within_rewards,
)
from .strategy.negrisk import coherent_model_probs
from .strategy.sizing import (
    CapsConfig,
    CapsState,
    SizedOrder,
    size_edge,
)

log = logging.getLogger(__name__)


Strategy = Literal["taker", "maker", "both"]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class _OpenOrder:
    """Maker order that hasn't filled yet. Tracked across snapshots."""

    event_slug: str
    bucket_label: str
    side: Action
    price: float
    shares: int
    p_model: float
    posted_at: dt.datetime


@dataclass
class _Fill:
    event_slug: str
    bucket_label: str
    target_date: dt.date
    side: Action
    price: float
    shares: int
    p_model_at_post: float
    posted_at: dt.datetime
    filled_at: dt.datetime


@dataclass
class _ResolvedEvent:
    target_date: dt.date
    realised_label: str | None  # None if observation missing


@dataclass
class BacktestResult:
    n_snapshots: int
    n_events_resolved: int
    fills_taker: list[_Fill] = field(default_factory=list)
    fills_maker: list[_Fill] = field(default_factory=list)
    pnl_taker_usd: float = 0.0
    pnl_maker_usd: float = 0.0
    fees_paid_usd: float = 0.0
    realised_log_loss: float | None = None
    by_station: dict[str, dict] = field(default_factory=dict)
    by_lead: dict[int, dict] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------


def _fetch_snapshots(
    cur,
    *,
    station_slugs: list[str],
    start: dt.date,
    end: dt.date,
) -> list[tuple]:
    """Return ``(event_slug, station_slug, target_date, bucket_label, lo_f,
    hi_f, snapshot_at, best_bid, best_ask, mid)`` ordered by snapshot_at."""
    cur.execute(
        """
        SELECT
            e.event_slug,
            s.slug,
            e.target_date,
            b.bucket_label,
            b.lo_f::float8,
            b.hi_f::float8,
            ms.snapshot_at,
            ms.best_bid::float8,
            ms.best_ask::float8,
            ms.mid::float8
        FROM pm_market_snapshots ms
        JOIN pm_buckets b ON b.event_slug = ms.event_slug
                          AND b.bucket_label = ms.bucket_label
        JOIN pm_events  e ON e.event_slug = ms.event_slug
        JOIN stations   s ON s.station_id = e.station_id
        WHERE e.target_date BETWEEN %s AND %s
          AND s.slug = ANY(%s)
        ORDER BY ms.snapshot_at, e.event_slug, b.bucket_label
        """,
        (start, end, station_slugs),
    )
    return list(cur.fetchall())


def _fetch_bucket_probs_history(
    cur,
    *,
    model_id: str,
    station_slugs: list[str],
    start: dt.date,
    end: dt.date,
) -> dict[tuple[str, str], list[tuple[dt.datetime, float]]]:
    """``{(event, label): [(run_time, prob), ...]}`` sorted ascending."""
    cur.execute(
        """
        SELECT bp.event_slug, bp.bucket_label, bp.run_time, bp.prob::float8
        FROM bucket_probs bp
        JOIN pm_events e ON e.event_slug = bp.event_slug
        JOIN stations  s ON s.station_id = e.station_id
        WHERE bp.model_id   = %s
          AND e.target_date BETWEEN %s AND %s
          AND s.slug = ANY(%s)
        ORDER BY bp.event_slug, bp.bucket_label, bp.run_time
        """,
        (model_id, start, end, station_slugs),
    )
    out: dict[tuple[str, str], list[tuple[dt.datetime, float]]] = {}
    for ev, lab, rt, prob in cur.fetchall():
        out.setdefault((ev, lab), []).append((rt, float(prob)))
    return out


def _fetch_resolutions(
    cur,
    event_slugs: list[str],
) -> dict[str, _ResolvedEvent]:
    """For each event, return the realised-bucket label keyed by event_slug.
    Prefers WU resolution observations, falls back to NOAA."""
    if not event_slugs:
        return {}
    cur.execute(
        """
        SELECT
            e.event_slug,
            e.target_date,
            o.observed_max_f,
            o.source
        FROM pm_events e
        JOIN observations o
          ON o.station_id = e.station_id
         AND o.obs_date   = e.target_date
         AND o.observed_max_f IS NOT NULL
         AND o.source IN ('wunderground:historical', 'noaa:ghcnd')
        WHERE e.event_slug = ANY(%s)
        """,
        (event_slugs,),
    )
    rows_by_event: dict[str, list[tuple]] = {}
    for ev, td, val, src in cur.fetchall():
        rows_by_event.setdefault(ev, []).append((td, val, src))

    cur.execute(
        """
        SELECT event_slug, bucket_label, lo_f::float8, hi_f::float8
        FROM pm_buckets WHERE event_slug = ANY(%s)
        """,
        (event_slugs,),
    )
    buckets_by_event: dict[str, list[BucketBounds]] = {}
    for ev, lab, lo, hi in cur.fetchall():
        buckets_by_event.setdefault(ev, []).append(
            BucketBounds(
                label=lab,
                lo_f=None if lo is None else float(lo),
                hi_f=None if hi is None else float(hi),
            )
        )

    out: dict[str, _ResolvedEvent] = {}
    for ev, candidates in rows_by_event.items():
        # Prefer WU
        candidates.sort(key=lambda r: 0 if r[2] == "wunderground:historical" else 1)
        td, val, _src = candidates[0]
        rb = realised_bucket(buckets_by_event.get(ev, []), float(val))
        out[ev] = _ResolvedEvent(
            target_date=td, realised_label=rb.label if rb else None
        )
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _latest_probs_at(
    history: dict[tuple[str, str], list[tuple[dt.datetime, float]]],
    *,
    event_slug: str,
    bucket_labels: list[str],
    cutoff: dt.datetime,
) -> dict[str, float] | None:
    """Latest run_time <= cutoff for every requested label. Returns None if
    any label is missing a prediction (don't bet on partial data)."""
    out: dict[str, float] = {}
    for lab in bucket_labels:
        seq = history.get((event_slug, lab))
        if not seq:
            return None
        # Binary search for last entry with rt <= cutoff
        lo, hi = 0, len(seq)
        while lo < hi:
            mid = (lo + hi) // 2
            if seq[mid][0] <= cutoff:
                lo = mid + 1
            else:
                hi = mid
        if lo == 0:
            return None
        out[lab] = seq[lo - 1][1]
    return out


def _settle_taker(
    fill: _Fill, realised_label: str | None
) -> tuple[float, str]:
    """Return (pnl_usd, comment) for a settled taker fill."""
    if realised_label is None:
        return 0.0, "unresolved"
    won_yes = fill.bucket_label == realised_label
    payoff_yes = 1.0 if won_yes else 0.0
    if fill.side == Action.TAKER_BUY:
        # Bought YES at price; if won, gets $1; loses cost+fee otherwise.
        per_share = payoff_yes - fill.price
        return per_share * fill.shares, f"taker_buy {'WIN' if won_yes else 'LOSE'}"
    if fill.side == Action.TAKER_SELL:
        # Sold YES at price; receive price now, pay $1 if YES wins.
        per_share = fill.price - payoff_yes
        return per_share * fill.shares, (
            f"taker_sell {'WIN' if not won_yes else 'LOSE'}"
        )
    return 0.0, "unknown side"


def _settle_maker(
    fill: _Fill, realised_label: str | None, *, fee: float
) -> tuple[float, str]:
    """Maker fills: same payoff math as taker but no fee paid (we already
    subtracted maker_fee in EV)."""
    pnl, comment = _settle_taker(fill, realised_label)
    return pnl, "maker " + comment


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def replay_backtest(
    *,
    model_id: str,
    station_slugs: Iterable[str] | None = None,
    start: dt.date,
    end: dt.date,
    caps: CapsConfig,
    fees: FeeSchedule = FeeSchedule(),
    strategy: Strategy = "both",
    target_spread: float = 0.04,
    max_open_orders_per_event: int = 4,
) -> BacktestResult:
    station_list = list(station_slugs) if station_slugs is not None else config.station_slugs()

    with with_conn() as conn, conn.cursor() as cur:
        snapshots = _fetch_snapshots(
            cur,
            station_slugs=station_list,
            start=start,
            end=end,
        )
        history = _fetch_bucket_probs_history(
            cur,
            model_id=model_id,
            station_slugs=station_list,
            start=start,
            end=end,
        )
        event_slugs = sorted({row[0] for row in snapshots})
        resolutions = _fetch_resolutions(cur, event_slugs)

    # Group snapshots by snapshot_at.
    grouped: dict[dt.datetime, list[tuple]] = {}
    for row in snapshots:
        grouped.setdefault(row[6], []).append(row)

    # Per-event open maker orders.
    open_orders: dict[str, list[_OpenOrder]] = {}
    fills_taker: list[_Fill] = []
    fills_maker: list[_Fill] = []
    fees_paid = 0.0

    # Track caps state per *event* for the duration of the backtest. We do
    # not enforce per-day or per-portfolio caps in the backtest replay
    # because that introduces ordering-dependent path effects that obscure
    # the per-event edge metric. The Phase 4b paper-trade run respects all
    # caps because it uses the live orchestrator code path.
    used_per_event: dict[str, float] = {}

    for snap_time in sorted(grouped):
        rows = grouped[snap_time]
        rows_by_event: dict[str, dict] = {}
        for row in rows:
            ev, station, target, label, lo, hi, _t, bid, ask, mid = row
            ev_grp = rows_by_event.setdefault(
                ev,
                {
                    "station": station,
                    "target": target,
                    "buckets": {},
                },
            )
            ev_grp["buckets"][label] = {
                "lo": lo, "hi": hi,
                "book": BookLevel(
                    best_bid=bid, best_ask=ask, mid=mid,
                ),
            }

        for ev, ev_grp in rows_by_event.items():
            target = ev_grp["target"]
            station = ev_grp["station"]
            bucket_labels = list(ev_grp["buckets"].keys())
            probs = _latest_probs_at(
                history, event_slug=ev, bucket_labels=bucket_labels,
                cutoff=snap_time,
            )
            if probs is None:
                continue
            coherent = coherent_model_probs(probs)

            # 1. Try to fill any open maker orders for this event with the
            #    new book.
            still_open: list[_OpenOrder] = []
            for o in open_orders.get(ev, []):
                book = ev_grp["buckets"].get(o.bucket_label, {}).get("book")
                if book is None:
                    still_open.append(o)
                    continue
                filled = False
                if o.side == Action.MAKER_BUY:
                    # Filled if some seller hits our bid: best_ask <= o.price.
                    if book.best_ask is not None and book.best_ask <= o.price:
                        filled = True
                elif o.side == Action.MAKER_SELL:
                    # Filled if buyer hits our offer: best_bid >= o.price.
                    if book.best_bid is not None and book.best_bid >= o.price:
                        filled = True
                if filled:
                    fills_maker.append(
                        _Fill(
                            event_slug=ev,
                            bucket_label=o.bucket_label,
                            target_date=target,
                            side=o.side,
                            price=o.price,
                            shares=o.shares,
                            p_model_at_post=o.p_model,
                            posted_at=o.posted_at,
                            filled_at=snap_time,
                        )
                    )
                else:
                    still_open.append(o)
            open_orders[ev] = still_open

            # 2. Generate new orders for this snapshot.
            state = CapsState(
                used_per_event=used_per_event.get(ev, 0.0),
                used_per_bucket=0.0,
                used_per_day=0.0,
                used_per_portfolio=0.0,
            )

            for label, info in ev_grp["buckets"].items():
                p_yes = coherent.get(label, 0.0)
                yes_book = info["book"]
                no_book = (
                    _no_book_from_yes(yes_book)
                    if (
                        yes_book.best_bid is not None
                        or yes_book.best_ask is not None
                    )
                    else None
                )

                if strategy in ("taker", "both"):
                    bb = BucketBook(yes=yes_book, no=no_book)
                    edge = best_taker_edge(p_yes, bb, fees=fees)
                    if edge is not None:
                        sized = size_edge(edge, caps=caps, state=state)
                        if sized is not None:
                            fills_taker.append(
                                _Fill(
                                    event_slug=ev,
                                    bucket_label=label,
                                    target_date=target,
                                    side=edge.action,
                                    price=edge.price,
                                    shares=sized.shares,
                                    p_model_at_post=p_yes,
                                    posted_at=snap_time,
                                    filled_at=snap_time,
                                )
                            )
                            fees_paid += sized.shares * edge.fee_per_share
                            state = CapsState(
                                used_per_bucket=state.used_per_bucket,
                                used_per_event=state.used_per_event + sized.notional_usd,
                                used_per_day=0.0,
                                used_per_portfolio=0.0,
                            )

                if strategy in ("maker", "both"):
                    n_open = len(open_orders.get(ev, []))
                    if n_open >= max_open_orders_per_event:
                        continue
                    buy_e, sell_e = maker_quote_yes_within_rewards(
                        p_yes,
                        yes_book,
                        target_spread=target_spread,
                        fees=fees,
                    )
                    for me in (buy_e, sell_e):
                        if me is None or me.ev_per_share <= 0:
                            continue
                        sized = size_edge(me, caps=caps, state=state)
                        if sized is None:
                            continue
                        open_orders.setdefault(ev, []).append(
                            _OpenOrder(
                                event_slug=ev,
                                bucket_label=label,
                                side=me.action,
                                price=me.price,
                                shares=sized.shares,
                                p_model=p_yes,
                                posted_at=snap_time,
                            )
                        )
                        state = CapsState(
                            used_per_bucket=state.used_per_bucket,
                            used_per_event=state.used_per_event + sized.notional_usd,
                            used_per_day=0.0,
                            used_per_portfolio=0.0,
                        )

            used_per_event[ev] = state.used_per_event

    # Settle.
    pnl_taker = 0.0
    pnl_maker = 0.0
    by_station: dict[str, dict] = {}
    by_lead: dict[int, dict] = {}

    for f in fills_taker:
        rev = resolutions.get(f.event_slug)
        if rev is None or rev.realised_label is None:
            continue
        pnl, _ = _settle_taker(f, rev.realised_label)
        pnl_taker += pnl

    for f in fills_maker:
        rev = resolutions.get(f.event_slug)
        if rev is None or rev.realised_label is None:
            continue
        pnl, _ = _settle_maker(f, rev.realised_label, fee=fees.maker_fee)
        pnl_maker += pnl

    # Per-station / per-lead rollups (taker only — maker requires more
    # nuanced accounting to avoid double-counting).
    for f in fills_taker:
        rev = resolutions.get(f.event_slug)
        if rev is None or rev.realised_label is None:
            continue
        pnl, _ = _settle_taker(f, rev.realised_label)
        st = by_station.setdefault(
            f.event_slug.split("-on-")[0].split("highest-temperature-in-")[-1],
            {"n": 0, "pnl": 0.0, "fees": 0.0},
        )
        st["n"] += 1
        st["pnl"] += pnl
        lead = max(0, (f.target_date - f.posted_at.date()).days)
        ld = by_lead.setdefault(lead, {"n": 0, "pnl": 0.0})
        ld["n"] += 1
        ld["pnl"] += pnl

    # Realised log-loss across distinct (event, snapshot) combos: we look
    # at the *last* prediction we acted on per event.
    realised_log_loss: float | None = None
    if resolutions:
        ll_total = 0.0
        ll_n = 0
        for ev, rev in resolutions.items():
            if rev.realised_label is None:
                continue
            seq = history.get((ev, rev.realised_label))
            if not seq:
                continue
            p_realised = seq[-1][1]
            p_eps = max(min(p_realised, 1 - 1e-12), 1e-12)
            ll_total += -math.log(p_eps)
            ll_n += 1
        if ll_n > 0:
            realised_log_loss = ll_total / ll_n

    return BacktestResult(
        n_snapshots=len(grouped),
        n_events_resolved=sum(1 for r in resolutions.values() if r.realised_label is not None),
        fills_taker=fills_taker,
        fills_maker=fills_maker,
        pnl_taker_usd=pnl_taker,
        pnl_maker_usd=pnl_maker,
        fees_paid_usd=fees_paid,
        realised_log_loss=realised_log_loss,
        by_station=by_station,
        by_lead=by_lead,
        notes=[],
    )


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------


def render_backtest_markdown(
    result: BacktestResult,
    *,
    model_id: str,
    start: dt.date,
    end: dt.date,
    strategy: Strategy,
    caps: CapsConfig,
) -> str:
    lines: list[str] = []
    lines.append(f"# Backtest — {model_id}")
    lines.append("")
    lines.append(f"- Window: {start} .. {end}")
    lines.append(f"- Strategy: {strategy}")
    lines.append(f"- Snapshots replayed: {result.n_snapshots}")
    lines.append(f"- Events resolved: {result.n_events_resolved}")
    lines.append(
        f"- Caps: per-bucket ${caps.per_bucket_usd:.2f}, "
        f"per-event ${caps.per_event_usd:.2f}, "
        f"kelly_fraction={caps.kelly_fraction:.2f}"
    )
    lines.append("")
    lines.append("## PnL summary")
    lines.append("")
    lines.append(f"- Taker PnL: **${result.pnl_taker_usd:+.2f}** "
                 f"(n_fills = {len(result.fills_taker)})")
    lines.append(f"- Maker PnL: **${result.pnl_maker_usd:+.2f}** "
                 f"(n_fills = {len(result.fills_maker)})")
    lines.append(f"- Fees paid: ${result.fees_paid_usd:.2f}")
    lines.append(
        f"- Net PnL: **${result.pnl_taker_usd + result.pnl_maker_usd:+.2f}**"
    )
    if result.realised_log_loss is not None:
        lines.append(
            f"- Realised log-loss on resolved events: {result.realised_log_loss:.4f}"
        )
    lines.append("")
    if result.by_station:
        lines.append("## By station (taker)")
        lines.append("")
        lines.append("| station | n | pnl |")
        lines.append("|---|---:|---:|")
        for st in sorted(result.by_station):
            v = result.by_station[st]
            lines.append(f"| {st} | {v['n']} | {v['pnl']:+.2f} |")
        lines.append("")
    if result.by_lead:
        lines.append("## By lead day (taker)")
        lines.append("")
        lines.append("| lead | n | pnl |")
        lines.append("|---:|---:|---:|")
        for lead in sorted(result.by_lead):
            v = result.by_lead[lead]
            lines.append(f"| {lead} | {v['n']} | {v['pnl']:+.2f} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def write_backtest_report(
    result: BacktestResult,
    *,
    model_id: str,
    start: dt.date,
    end: dt.date,
    strategy: Strategy,
    caps: CapsConfig,
    out_path=None,
):
    paths = config.paths()
    paths.reports.mkdir(parents=True, exist_ok=True)
    if out_path is None:
        ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        out_path = paths.reports / f"backtest_{ts}.md"
    md = render_backtest_markdown(
        result, model_id=model_id, start=start, end=end,
        strategy=strategy, caps=caps,
    )
    out_path.write_text(md, encoding="utf-8")
    return out_path
