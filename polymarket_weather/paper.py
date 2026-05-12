"""Paper-trading shadow ledger.

Run the same recommendation pipeline as ``cli.recommend``, but instead of
just printing a markdown report, persist every recommended order to
``paper_trades``. A separate ``settle`` step looks up resolved events,
matches each paper trade to its realised bucket, and records realised PnL.

This lets us measure realised log-loss + cumulative PnL after fees on a
running portfolio basis *before* exposing any capital.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass

from . import config
from .db import with_conn
from .recommend import EventRecommendation, build_recommendations
from .score import BucketBounds, realised_bucket
from .strategy.edge import Action, FeeSchedule
from .strategy.sizing import CapsConfig, SizedOrder, tiny_bankroll_caps

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------


INSERT_PAPER_SQL = """
INSERT INTO paper_trades
    (posted_at, event_slug, bucket_label, target_date, side, token_id,
     price, shares, notional_usd, p_model_at_post, expected_value_usd,
     fee_per_share, model_id, model_run_time, notes)
VALUES (now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def _persist_order(
    cur,
    *,
    event_slug: str,
    bucket_label: str,
    target_date: dt.date,
    side: Action,
    sized: SizedOrder,
    p_model: float,
    model_id: str,
    model_run_time: dt.datetime | None,
    token_id: str | None = None,
) -> None:
    cur.execute(
        INSERT_PAPER_SQL,
        (
            event_slug,
            bucket_label,
            target_date,
            side.value,
            token_id,
            sized.edge.price,
            sized.shares,
            sized.notional_usd,
            p_model,
            sized.expected_value_usd,
            sized.edge.fee_per_share,
            model_id,
            model_run_time,
            sized.notes,
        ),
    )


def _yes_token(cur, event_slug: str, bucket_label: str) -> str | None:
    cur.execute(
        "SELECT yes_token_id FROM pm_buckets WHERE event_slug=%s AND bucket_label=%s",
        (event_slug, bucket_label),
    )
    row = cur.fetchone()
    return row[0] if row else None


def submit_paper_trades(
    recs: list[EventRecommendation],
    *,
    persist_taker: bool = True,
    persist_maker: bool = True,
) -> int:
    """Persist all best-taker / maker orders from a recommendations payload."""
    n = 0
    with with_conn() as conn, conn.cursor() as cur:
        for r in recs:
            for b in r.buckets:
                token = _yes_token(cur, r.event_slug, b.bucket_label)
                if persist_taker and b.best_taker is not None:
                    _persist_order(
                        cur,
                        event_slug=r.event_slug,
                        bucket_label=b.bucket_label,
                        target_date=r.target_date,
                        side=b.best_taker.edge.action,
                        sized=b.best_taker,
                        p_model=b.p_model,
                        model_id=r.model_id,
                        model_run_time=r.model_run_time,
                        token_id=token,
                    )
                    n += 1
                if persist_maker and b.maker_buy is not None:
                    _persist_order(
                        cur,
                        event_slug=r.event_slug,
                        bucket_label=b.bucket_label,
                        target_date=r.target_date,
                        side=Action.MAKER_BUY,
                        sized=b.maker_buy,
                        p_model=b.p_model,
                        model_id=r.model_id,
                        model_run_time=r.model_run_time,
                        token_id=token,
                    )
                    n += 1
                if persist_maker and b.maker_sell is not None:
                    _persist_order(
                        cur,
                        event_slug=r.event_slug,
                        bucket_label=b.bucket_label,
                        target_date=r.target_date,
                        side=Action.MAKER_SELL,
                        sized=b.maker_sell,
                        p_model=b.p_model,
                        model_id=r.model_id,
                        model_run_time=r.model_run_time,
                        token_id=token,
                    )
                    n += 1
    return n


def run_paper_session(
    *,
    station_slugs: list[str] | None = None,
    days_ahead: int = 7,
    primary_model: str | None = None,
    fallback_model: str | None = None,
    caps: CapsConfig | None = None,
    persist_taker: bool = True,
    persist_maker: bool = True,
) -> int:
    """Build recommendations and persist into ``paper_trades``."""
    from .models.baseline import MODEL_M1
    from .models.m2_postprocessed_ensemble import MODEL_M2

    primary_model = primary_model or MODEL_M2
    fallback_model = fallback_model or MODEL_M1
    caps = caps or tiny_bankroll_caps()
    recs = build_recommendations(
        station_slugs=station_slugs,
        days_ahead=days_ahead,
        primary_model=primary_model,
        fallback_model=fallback_model,
        caps=caps,
        fees=FeeSchedule(),
    )
    return submit_paper_trades(
        recs, persist_taker=persist_taker, persist_maker=persist_maker
    )


# ---------------------------------------------------------------------------
# Settle
# ---------------------------------------------------------------------------


@dataclass
class SettleResult:
    n_settled: int
    n_skipped_unresolved: int
    pnl_total_usd: float
    realised_log_loss: float | None


def settle_paper_trades(
    *,
    older_than_days: int = 0,
) -> SettleResult:
    """Mark all unsettled paper trades whose event has resolved.

    ``older_than_days`` adds a safety buffer — only settle events whose
    target_date is at least N days old (so we don't settle on a same-day
    NOAA observation that hasn't finalised yet).
    """
    cutoff = dt.date.today() - dt.timedelta(days=older_than_days)
    pnl_total = 0.0
    n_settled = 0
    n_skipped = 0
    log_loss_sum = 0.0
    log_loss_n = 0

    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                pt.paper_trade_id,
                pt.event_slug,
                pt.bucket_label,
                pt.target_date,
                pt.side,
                pt.price,
                pt.shares,
                pt.p_model_at_post,
                pt.expected_value_usd
            FROM paper_trades pt
            WHERE pt.settled_at IS NULL
              AND pt.target_date <= %s
            """,
            (cutoff,),
        )
        unsettled = list(cur.fetchall())
        if not unsettled:
            return SettleResult(0, 0, 0.0, None)

        # Build resolution lookup: prefer WU then NOAA observation per event.
        slugs = sorted({r[1] for r in unsettled})
        cur.execute(
            """
            SELECT e.event_slug, e.target_date, o.observed_max_f, o.source
            FROM pm_events e
            JOIN observations o
              ON o.station_id = e.station_id
             AND o.obs_date   = e.target_date
             AND o.observed_max_f IS NOT NULL
             AND o.finalized   = TRUE
             AND o.source IN ('wunderground:historical', 'noaa:ghcnd')
            WHERE e.event_slug = ANY(%s)
            """,
            (slugs,),
        )
        rows_by_event: dict[str, list[tuple]] = {}
        for ev, td, val, src in cur.fetchall():
            rows_by_event.setdefault(ev, []).append((td, float(val), src))

        cur.execute(
            """
            SELECT event_slug, bucket_label, lo_f::float8, hi_f::float8
            FROM pm_buckets WHERE event_slug = ANY(%s)
            """,
            (slugs,),
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

        realised_by_event: dict[str, str | None] = {}
        for ev in slugs:
            candidates = rows_by_event.get(ev, [])
            if not candidates:
                realised_by_event[ev] = None
                continue
            candidates.sort(
                key=lambda r: 0 if r[2] == "wunderground:historical" else 1
            )
            _td, val, _src = candidates[0]
            rb = realised_bucket(buckets_by_event.get(ev, []), val)
            realised_by_event[ev] = rb.label if rb else None

        for (
            paper_id,
            event_slug,
            bucket_label,
            _target_date,
            side,
            price,
            shares,
            p_model_at_post,
            _ev_expected,
        ) in unsettled:
            realised_label = realised_by_event.get(event_slug)
            if realised_label is None:
                n_skipped += 1
                continue
            won = bucket_label == realised_label
            payoff = 1.0 if won else 0.0
            if side in ("taker_buy", "maker_buy"):
                per_share = payoff - float(price)
                # Fee already accounted for in expected_value_usd, but for
                # realised PnL we re-deduct taker fee explicitly because
                # ``realised_pnl_usd`` is gross-of-fees in our ledger.
                fee_per_share = (
                    0.05 * float(price) if side == "taker_buy" else 0.0
                )
                pnl = per_share * shares - fee_per_share * shares
            elif side in ("taker_sell", "maker_sell"):
                per_share = float(price) - payoff
                fee_per_share = (
                    0.05 * float(price) if side == "taker_sell" else 0.0
                )
                pnl = per_share * shares - fee_per_share * shares
            else:
                pnl = 0.0
            pnl_total += pnl
            n_settled += 1

            # Realised log-loss contribution for the realised bucket,
            # weighted by the prediction we acted on.
            if won:
                p_eps = max(min(float(p_model_at_post), 1 - 1e-12), 1e-12)
                log_loss_sum += -math.log(p_eps)
                log_loss_n += 1

            cur.execute(
                """
                UPDATE paper_trades
                SET filled_at = COALESCE(filled_at, posted_at),
                    realised_label = %s,
                    realised_pnl_usd = %s,
                    settled_at = now()
                WHERE paper_trade_id = %s
                """,
                (realised_label, pnl, paper_id),
            )

    return SettleResult(
        n_settled=n_settled,
        n_skipped_unresolved=n_skipped,
        pnl_total_usd=pnl_total,
        realised_log_loss=(
            log_loss_sum / log_loss_n if log_loss_n > 0 else None
        ),
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_paper_summary_markdown(
    *,
    days: int = 30,
) -> str:
    cutoff = dt.date.today() - dt.timedelta(days=days)
    lines: list[str] = []
    lines.append(f"# Paper trading summary — last {days} days")
    lines.append("")

    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE settled_at IS NOT NULL),
                COUNT(*) FILTER (WHERE settled_at IS NULL),
                COALESCE(SUM(realised_pnl_usd) FILTER (WHERE settled_at IS NOT NULL), 0),
                COALESCE(SUM(notional_usd) FILTER (WHERE settled_at IS NOT NULL), 0),
                COALESCE(SUM(expected_value_usd) FILTER (WHERE settled_at IS NOT NULL), 0)
            FROM paper_trades
            WHERE target_date >= %s
            """,
            (cutoff,),
        )
        n_settled, n_open, pnl, notional, ev_total = cur.fetchone()
        lines.append(f"- Settled trades: {n_settled}")
        lines.append(f"- Open trades:    {n_open}")
        lines.append(
            f"- Notional traded: ${float(notional):.2f} | "
            f"Realised PnL: ${float(pnl):+.2f} | "
            f"Predicted EV at post: ${float(ev_total):+.2f}"
        )
        lines.append("")

        cur.execute(
            """
            SELECT
                substring(event_slug from 'highest-temperature-in-([^-]+)') AS station,
                COUNT(*) FILTER (WHERE settled_at IS NOT NULL),
                SUM(realised_pnl_usd) FILTER (WHERE settled_at IS NOT NULL)
            FROM paper_trades
            WHERE target_date >= %s
            GROUP BY 1
            ORDER BY 3 DESC NULLS LAST
            """,
            (cutoff,),
        )
        lines.append("## By station")
        lines.append("")
        lines.append("| station | n_settled | pnl |")
        lines.append("|---|---:|---:|")
        for st, n, p in cur.fetchall():
            lines.append(
                f"| {st or '?'} | {int(n)} | "
                f"{float(p) if p is not None else 0.0:+.2f} |"
            )

        cur.execute(
            """
            SELECT
                side,
                COUNT(*) FILTER (WHERE settled_at IS NOT NULL),
                SUM(realised_pnl_usd) FILTER (WHERE settled_at IS NOT NULL)
            FROM paper_trades
            WHERE target_date >= %s
            GROUP BY 1
            ORDER BY 1
            """,
            (cutoff,),
        )
        lines.append("")
        lines.append("## By side")
        lines.append("")
        lines.append("| side | n_settled | pnl |")
        lines.append("|---|---:|---:|")
        for side, n, p in cur.fetchall():
            lines.append(
                f"| {side} | {int(n)} | "
                f"{float(p) if p is not None else 0.0:+.2f} |"
            )

    return "\n".join(lines) + "\n"
