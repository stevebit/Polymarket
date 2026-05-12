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

Output: ``BacktestResult`` with per-strategy PnL, realised log-loss, fill
metrics, **snapshot spacing stats**, **gross equity curve**, and optional
JSON export for ``cli.backtest_dashboard``. The CLI ``cli.backtest`` writes a
markdown report and can ``--export-json`` / ``--take-every-n-snapshots``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

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
    station_slug: str
    bucket_label: str
    bucket_lo_f: float | None
    bucket_hi_f: float | None
    side: Action
    price: float
    shares: int
    p_model: float
    expected_pnl_per_share_at_post: float
    posted_at: dt.datetime


@dataclass
class _Fill:
    event_slug: str
    station_slug: str
    bucket_label: str
    bucket_lo_f: float | None
    bucket_hi_f: float | None
    target_date: dt.date
    side: Action
    price: float
    shares: int
    p_model_at_post: float
    expected_pnl_per_share_at_post: float
    fee_usd: float
    posted_at: dt.datetime
    filled_at: dt.datetime
    # Filled in by the settlement pass below. ``realised_label`` is None when
    # we did not have a resolution observation for the event in the window.
    realised_label: str | None = None
    realised_pnl_usd: float = 0.0


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
    # Richer diagnostics (Phase 4+ backtest environment improvements)
    snapshot_stats: dict[str, Any] = field(default_factory=dict)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    max_drawdown_usd: float = 0.0
    take_every_n_snapshots: int = 1


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
         AND o.finalized   = TRUE
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


def _snapshot_time_stats(times: list[dt.datetime]) -> dict[str, Any]:
    if len(times) < 2:
        return {
            "n_snapshots": len(times),
            "median_hours_between": None,
            "mean_hours_between": None,
            "first_snapshot_at": times[0].isoformat() if times else None,
            "last_snapshot_at": times[-1].isoformat() if times else None,
        }
    diffs = [
        (times[i] - times[i - 1]).total_seconds() / 3600.0
        for i in range(1, len(times))
    ]
    return {
        "n_snapshots": len(times),
        "median_hours_between": float(statistics.median(diffs)),
        "mean_hours_between": float(sum(diffs) / len(diffs)),
        "first_snapshot_at": times[0].isoformat(),
        "last_snapshot_at": times[-1].isoformat(),
    }


def _build_equity_curve(
    fills_taker: list[_Fill],
    fills_maker: list[_Fill],
    resolutions: dict[str, _ResolvedEvent],
    *,
    fees: FeeSchedule,
) -> tuple[list[dict[str, Any]], float]:
    """Cumulative **gross** settled PnL (payoff vs price) ordered by fill time.

    Exchange taker fees are tracked separately in ``fees_paid_usd`` on the
    result object; subtract them in dashboards if you want a net-of-fee curve.
    """
    events: list[tuple[dt.datetime, float, str]] = []
    for f in fills_taker:
        rev = resolutions.get(f.event_slug)
        if rev is None or rev.realised_label is None:
            continue
        pnl, _ = _settle_taker(f, rev.realised_label)
        events.append((f.filled_at, pnl, "taker"))
    for f in fills_maker:
        rev = resolutions.get(f.event_slug)
        if rev is None or rev.realised_label is None:
            continue
        pnl, _ = _settle_maker(f, rev.realised_label, fee=fees.maker_fee)
        events.append((f.filled_at, pnl, "maker"))
    events.sort(key=lambda x: x[0])
    curve: list[dict[str, Any]] = []
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for ts, pnl, kind in events:
        cum += pnl
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
        curve.append(
            {
                "filled_at": ts.isoformat(),
                "kind": kind,
                "incremental_pnl_usd": round(pnl, 6),
                "cumulative_pnl_usd": round(cum, 6),
            }
        )
    return curve, float(max_dd)


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
    take_every_n_snapshots: int = 1,
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

    snap_times = sorted(grouped.keys())
    take_n = max(1, int(take_every_n_snapshots))
    if take_n > 1:
        snap_times = snap_times[::take_n]
    snapshot_stats = _snapshot_time_stats(snap_times)
    snapshot_stats["take_every_n_snapshots"] = take_n
    snapshot_stats["total_snapshots_in_db"] = len(grouped)

    # Per-event open maker orders.
    open_orders: dict[str, list[_OpenOrder]] = {}
    fills_taker: list[_Fill] = []
    fills_maker: list[_Fill] = []
    fees_paid = 0.0

    # Fill-rate / opportunity counters surfaced in snapshot_stats so dashboards
    # can compute "did we trade often, and was it because of caps or no edge".
    n_event_snapshots = 0
    n_event_snapshots_with_probs = 0
    n_bucket_opportunities = 0
    n_taker_edges_found = 0
    n_taker_filled = 0
    n_maker_quotes_attempted = 0
    n_maker_orders_posted = 0
    n_maker_orders_filled = 0

    # Track caps state per *event* for the duration of the backtest. We do
    # not enforce per-day or per-portfolio caps in the backtest replay
    # because that introduces ordering-dependent path effects that obscure
    # the per-event edge metric. The Phase 4b paper-trade run respects all
    # caps because it uses the live orchestrator code path.
    used_per_event: dict[str, float] = {}

    for snap_time in snap_times:
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
            n_event_snapshots += 1
            probs = _latest_probs_at(
                history, event_slug=ev, bucket_labels=bucket_labels,
                cutoff=snap_time,
            )
            if probs is None:
                continue
            n_event_snapshots_with_probs += 1
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
                            station_slug=o.station_slug,
                            bucket_label=o.bucket_label,
                            bucket_lo_f=o.bucket_lo_f,
                            bucket_hi_f=o.bucket_hi_f,
                            target_date=target,
                            side=o.side,
                            price=o.price,
                            shares=o.shares,
                            p_model_at_post=o.p_model,
                            expected_pnl_per_share_at_post=o.expected_pnl_per_share_at_post,
                            fee_usd=0.0,
                            posted_at=o.posted_at,
                            filled_at=snap_time,
                        )
                    )
                    n_maker_orders_filled += 1
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
                n_bucket_opportunities += 1
                p_yes = coherent.get(label, 0.0)
                yes_book = info["book"]
                bucket_lo = info.get("lo")
                bucket_hi = info.get("hi")
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
                        n_taker_edges_found += 1
                        sized = size_edge(edge, caps=caps, state=state)
                        if sized is not None:
                            fee_total = sized.shares * edge.fee_per_share
                            fills_taker.append(
                                _Fill(
                                    event_slug=ev,
                                    station_slug=station,
                                    bucket_label=label,
                                    bucket_lo_f=bucket_lo,
                                    bucket_hi_f=bucket_hi,
                                    target_date=target,
                                    side=edge.action,
                                    price=edge.price,
                                    shares=sized.shares,
                                    p_model_at_post=p_yes,
                                    expected_pnl_per_share_at_post=edge.ev_per_share,
                                    fee_usd=fee_total,
                                    posted_at=snap_time,
                                    filled_at=snap_time,
                                )
                            )
                            fees_paid += fee_total
                            n_taker_filled += 1
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
                    n_maker_quotes_attempted += 1
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
                                station_slug=station,
                                bucket_label=label,
                                bucket_lo_f=bucket_lo,
                                bucket_hi_f=bucket_hi,
                                side=me.action,
                                price=me.price,
                                shares=sized.shares,
                                p_model=p_yes,
                                expected_pnl_per_share_at_post=me.ev_per_share,
                                posted_at=snap_time,
                            )
                        )
                        n_maker_orders_posted += 1
                        state = CapsState(
                            used_per_bucket=state.used_per_bucket,
                            used_per_event=state.used_per_event + sized.notional_usd,
                            used_per_day=0.0,
                            used_per_portfolio=0.0,
                        )

            used_per_event[ev] = state.used_per_event

    # Settle. Mutate each fill in-place with realised_label and realised_pnl_usd
    # so JSON export and dashboards have per-fill outcomes without a second
    # DB hit.
    pnl_taker = 0.0
    pnl_maker = 0.0
    by_station: dict[str, dict] = {}
    by_lead: dict[int, dict] = {}

    for f in fills_taker:
        rev = resolutions.get(f.event_slug)
        if rev is None or rev.realised_label is None:
            continue
        pnl, _ = _settle_taker(f, rev.realised_label)
        f.realised_label = rev.realised_label
        f.realised_pnl_usd = pnl
        pnl_taker += pnl
        slug = f.station_slug or "unknown"
        st = by_station.setdefault(slug, {"n": 0, "pnl": 0.0, "fees": 0.0})
        st["n"] += 1
        st["pnl"] += pnl
        st["fees"] += f.fee_usd
        lead = max(0, (f.target_date - f.posted_at.date()).days)
        ld = by_lead.setdefault(lead, {"n": 0, "pnl": 0.0})
        ld["n"] += 1
        ld["pnl"] += pnl

    for f in fills_maker:
        rev = resolutions.get(f.event_slug)
        if rev is None or rev.realised_label is None:
            continue
        pnl, _ = _settle_maker(f, rev.realised_label, fee=fees.maker_fee)
        f.realised_label = rev.realised_label
        f.realised_pnl_usd = pnl
        pnl_maker += pnl

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

    equity_curve, max_dd = _build_equity_curve(
        fills_taker, fills_maker, resolutions, fees=fees,
    )

    # Fill-rate diagnostics for "did we have edge but skip due to caps?"
    snapshot_stats["n_event_snapshots"] = n_event_snapshots
    snapshot_stats["n_event_snapshots_with_probs"] = n_event_snapshots_with_probs
    snapshot_stats["n_bucket_opportunities"] = n_bucket_opportunities
    snapshot_stats["n_taker_edges_found"] = n_taker_edges_found
    snapshot_stats["n_taker_filled"] = n_taker_filled
    snapshot_stats["n_maker_quotes_attempted"] = n_maker_quotes_attempted
    snapshot_stats["n_maker_orders_posted"] = n_maker_orders_posted
    snapshot_stats["n_maker_orders_filled"] = n_maker_orders_filled
    snapshot_stats["taker_fill_rate"] = (
        n_taker_filled / n_taker_edges_found if n_taker_edges_found else None
    )
    snapshot_stats["maker_fill_rate"] = (
        n_maker_orders_filled / n_maker_orders_posted
        if n_maker_orders_posted
        else None
    )

    notes_bt: list[str] = []
    if take_n > 1:
        notes_bt.append(
            f"Replay used every {take_n}th snapshot chronologically "
            f"({len(snap_times)} of {len(grouped)} total) — coarser book path, "
            "useful when stress-testing maker fill assumptions."
        )

    return BacktestResult(
        n_snapshots=len(snap_times),
        n_events_resolved=sum(1 for r in resolutions.values() if r.realised_label is not None),
        fills_taker=fills_taker,
        fills_maker=fills_maker,
        pnl_taker_usd=pnl_taker,
        pnl_maker_usd=pnl_maker,
        fees_paid_usd=fees_paid,
        realised_log_loss=realised_log_loss,
        by_station=by_station,
        by_lead=by_lead,
        notes=notes_bt,
        snapshot_stats=snapshot_stats,
        equity_curve=equity_curve,
        max_drawdown_usd=max_dd,
        take_every_n_snapshots=take_n,
    )


def _fill_to_dict(f: _Fill) -> dict[str, Any]:
    """Serialise a fill with all post-time and settled fields the dashboards need.

    ``realised_pnl_usd`` is the gross settlement payoff (does **not** subtract
    ``fee_usd``); the dashboard subtracts ``fee_usd`` when it wants net PnL so
    fees stay attributable to fills rather than being lumped into a global
    bucket. ``expected_pnl_per_share_at_post`` already nets the relevant fee.
    """
    lead_days = max(0, (f.target_date - f.posted_at.date()).days)
    return {
        "event_slug": f.event_slug,
        "station_slug": f.station_slug,
        "bucket_label": f.bucket_label,
        "bucket_lo_f": f.bucket_lo_f,
        "bucket_hi_f": f.bucket_hi_f,
        "target_date": f.target_date.isoformat(),
        "lead_days": lead_days,
        "side": f.side.value,
        "price": f.price,
        "shares": f.shares,
        "notional_usd": round(f.price * f.shares, 6),
        "p_model_at_post": f.p_model_at_post,
        "expected_pnl_per_share_at_post": f.expected_pnl_per_share_at_post,
        "fee_usd": round(f.fee_usd, 6),
        "posted_at": f.posted_at.isoformat(),
        "filled_at": f.filled_at.isoformat(),
        "realised_label": f.realised_label,
        "realised_pnl_usd": round(f.realised_pnl_usd, 6),
        "settled": f.realised_label is not None,
        "won": (
            None
            if f.realised_label is None
            else (f.realised_label == f.bucket_label)
        ),
    }


def backtest_result_to_dict(
    result: BacktestResult,
    *,
    model_id: str,
    start: dt.date,
    end: dt.date,
    strategy: Strategy,
    caps: CapsConfig,
    fees: FeeSchedule | None = None,
) -> dict[str, Any]:
    """JSON-serialisable bundle for ``backtest_dashboard`` and external tooling."""
    fees = fees or FeeSchedule()
    net = result.pnl_taker_usd + result.pnl_maker_usd - result.fees_paid_usd
    return {
        "meta": {
            "export_version": 2,
            "model_id": model_id,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "strategy": strategy,
            "caps": {
                "bankroll_usd": caps.bankroll_usd,
                "per_bucket_usd": caps.per_bucket_usd,
                "per_event_usd": caps.per_event_usd,
                "per_day_usd": caps.per_day_usd,
                "per_portfolio_usd": caps.per_portfolio_usd,
                "kelly_fraction": caps.kelly_fraction,
                "min_edge_per_dollar": caps.min_edge_per_dollar,
            },
            "fees": {"taker_fee": fees.taker_fee, "maker_fee": fees.maker_fee},
        },
        "summary": {
            "n_snapshots": result.n_snapshots,
            "n_events_resolved": result.n_events_resolved,
            "n_fills_taker": len(result.fills_taker),
            "n_fills_maker": len(result.fills_maker),
            "pnl_taker_usd": result.pnl_taker_usd,
            "pnl_maker_usd": result.pnl_maker_usd,
            "fees_paid_usd": result.fees_paid_usd,
            "net_pnl_usd": net,
            "realised_log_loss": result.realised_log_loss,
            "max_drawdown_usd": result.max_drawdown_usd,
            "take_every_n_snapshots": result.take_every_n_snapshots,
        },
        "snapshot_stats": result.snapshot_stats,
        "equity_curve": result.equity_curve,
        "by_station": result.by_station,
        "by_lead": result.by_lead,
        "fills_taker": [_fill_to_dict(f) for f in result.fills_taker],
        "fills_maker": [_fill_to_dict(f) for f in result.fills_maker],
        "notes": result.notes,
    }


def export_backtest_json(
    path: str | Path,
    payload: dict[str, Any],
) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return p


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
    net = result.pnl_taker_usd + result.pnl_maker_usd - result.fees_paid_usd
    lines.append(
        f"- Net PnL (after taker fees): **${net:+.2f}** "
        f"(gross ${result.pnl_taker_usd + result.pnl_maker_usd:+.2f} − fees)"
    )
    if result.max_drawdown_usd != 0.0:
        lines.append(
            f"- Max drawdown on fee-adjusted equity path: **${result.max_drawdown_usd:+.2f}**"
        )
    if result.snapshot_stats:
        lines.append("")
        lines.append("## Snapshot coverage")
        lines.append("")
        ss = result.snapshot_stats
        lines.append(f"- Snapshots used in replay: **{ss.get('n_snapshots', 0)}**")
        lines.append(
            f"- Total snapshot timestamps in DB window: **{ss.get('total_snapshots_in_db', 0)}**"
        )
        if ss.get("median_hours_between") is not None:
            lines.append(
                f"- Median hours between consecutive **used** snapshots: "
                f"{ss['median_hours_between']:.2f} h"
            )
        if ss.get("take_every_n_snapshots", 1) > 1:
            lines.append(
                f"- Subsample: every **{ss['take_every_n_snapshots']}** snapshot(s)"
            )
    if result.notes:
        lines.append("")
        lines.append("## Notes")
        lines.append("")
        for n in result.notes:
            lines.append(f"- {n}")
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
