"""Order / fill / PnL state persistence and reconciliation.

This is the durable mirror of what the CLOB knows about our portfolio. The
orchestrator reconciles on every tick by listing open orders from the
exchange and updating local state.

Reconciliation rules:

1. Any local ``orders`` row with status ∈ {open, partially_filled} that
   isn't returned by ``client.get_orders()`` is assumed to be cancelled or
   filled. We mark such rows as ``status='unknown'`` and let the next call
   to ``record_fill`` (or a manual investigation) reclassify.
2. Any open order returned by the CLOB but missing locally (e.g. placed
   from another tool) is recorded with ``client_order_id=NULL``.
3. Fills are dedup'd by ``fill_id``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass

from ..db import with_conn

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class OrderRecord:
    order_id: str
    client_order_id: str | None
    event_slug: str
    bucket_label: str
    target_date: dt.date
    side: str            # 'maker_buy', 'maker_sell', 'taker_buy', 'taker_sell'
    token_id: str
    price: float
    requested_shares: int
    filled_shares: int = 0
    cancelled_shares: int = 0
    status: str = "open"
    p_model_at_post: float | None = None
    expected_value_usd: float | None = None
    model_id: str | None = None
    model_run_time: dt.datetime | None = None
    notes: str | None = None
    raw: dict | None = None


@dataclass
class FillRecord:
    fill_id: str
    order_id: str
    filled_at: dt.datetime
    side: str
    token_id: str
    price: float
    shares: int
    fee_usd: float = 0.0
    raw: dict | None = None


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def new_client_order_id() -> str:
    return f"pwx-{uuid.uuid4().hex[:16]}"


UPSERT_ORDER_SQL = """
INSERT INTO orders
    (order_id, client_order_id, posted_at, last_seen_at,
     event_slug, bucket_label, target_date, side, token_id, price,
     requested_shares, filled_shares, cancelled_shares, status,
     p_model_at_post, expected_value_usd, model_id, model_run_time, notes, raw)
VALUES (%s, %s, now(), now(), %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
ON CONFLICT (order_id) DO UPDATE SET
    last_seen_at      = now(),
    filled_shares     = EXCLUDED.filled_shares,
    cancelled_shares  = EXCLUDED.cancelled_shares,
    status            = EXCLUDED.status,
    raw               = EXCLUDED.raw
"""


def record_order(order: OrderRecord) -> None:
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            UPSERT_ORDER_SQL,
            (
                order.order_id,
                order.client_order_id,
                order.event_slug,
                order.bucket_label,
                order.target_date,
                order.side,
                order.token_id,
                order.price,
                order.requested_shares,
                order.filled_shares,
                order.cancelled_shares,
                order.status,
                order.p_model_at_post,
                order.expected_value_usd,
                order.model_id,
                order.model_run_time,
                order.notes,
                json.dumps(order.raw or {}),
            ),
        )


UPSERT_FILL_SQL = """
INSERT INTO fills
    (fill_id, order_id, filled_at, side, token_id, price, shares, fee_usd, raw)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
ON CONFLICT (fill_id) DO NOTHING
"""


def record_fill(fill: FillRecord) -> None:
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            UPSERT_FILL_SQL,
            (
                fill.fill_id,
                fill.order_id,
                fill.filled_at,
                fill.side,
                fill.token_id,
                fill.price,
                fill.shares,
                fill.fee_usd,
                json.dumps(fill.raw or {}),
            ),
        )


def update_order_status(
    order_id: str, *, status: str, filled_shares: int | None = None
) -> None:
    with with_conn() as conn, conn.cursor() as cur:
        if filled_shares is None:
            cur.execute(
                "UPDATE orders SET status=%s, last_seen_at=now() WHERE order_id=%s",
                (status, order_id),
            )
        else:
            cur.execute(
                """UPDATE orders
                   SET status=%s, filled_shares=%s, last_seen_at=now()
                   WHERE order_id=%s""",
                (status, filled_shares, order_id),
            )


# ---------------------------------------------------------------------------
# Reads / aggregates
# ---------------------------------------------------------------------------


def list_open_orders() -> list[dict]:
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT order_id, client_order_id, event_slug, bucket_label,
                   target_date, side, token_id, price, requested_shares,
                   filled_shares, cancelled_shares, status
            FROM orders
            WHERE status IN ('open', 'partially_filled')
            ORDER BY posted_at DESC
            """
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def daily_notional_used(day: dt.date) -> float:
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(price::float8 * requested_shares), 0)
            FROM orders
            WHERE DATE(posted_at AT TIME ZONE 'UTC') = %s
              AND status IN ('open', 'partially_filled', 'filled')
            """,
            (day,),
        )
        row = cur.fetchone()
        return float(row[0]) if row else 0.0


def event_notional_used(event_slug: str) -> float:
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(price::float8 * requested_shares), 0)
            FROM orders
            WHERE event_slug = %s
              AND status IN ('open', 'partially_filled', 'filled')
            """,
            (event_slug,),
        )
        row = cur.fetchone()
        return float(row[0]) if row else 0.0


def bucket_notional_used(event_slug: str, bucket_label: str) -> float:
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(price::float8 * requested_shares), 0)
            FROM orders
            WHERE event_slug = %s
              AND bucket_label = %s
              AND status IN ('open', 'partially_filled', 'filled')
            """,
            (event_slug, bucket_label),
        )
        row = cur.fetchone()
        return float(row[0]) if row else 0.0


def portfolio_notional_used() -> float:
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(price::float8 * (requested_shares - filled_shares - cancelled_shares)), 0)
            FROM orders
            WHERE status IN ('open', 'partially_filled')
            """,
        )
        row = cur.fetchone()
        return float(row[0]) if row else 0.0


# ---------------------------------------------------------------------------
# Daily PnL refresh
# ---------------------------------------------------------------------------


REFRESH_DAILY_PNL_SQL = """
INSERT INTO daily_pnl
    (pnl_date, notional_traded_usd, realised_pnl_usd, fees_paid_usd,
     rewards_earned_usd, open_orders_count, fills_count, refreshed_at)
SELECT
    %s::date AS pnl_date,
    COALESCE(SUM(f.price::float8 * f.shares), 0) AS notional_traded,
    0 AS realised_pnl,            -- settled separately when events resolve
    COALESCE(SUM(f.fee_usd), 0) AS fees_paid,
    0 AS rewards_earned,          -- populated from external rewards feed
    (SELECT COUNT(*) FROM orders WHERE status IN ('open', 'partially_filled')),
    COUNT(*) AS fills_count,
    now()
FROM fills f
WHERE DATE(f.filled_at AT TIME ZONE 'UTC') = %s
ON CONFLICT (pnl_date) DO UPDATE SET
    notional_traded_usd = EXCLUDED.notional_traded_usd,
    fees_paid_usd       = EXCLUDED.fees_paid_usd,
    open_orders_count   = EXCLUDED.open_orders_count,
    fills_count         = EXCLUDED.fills_count,
    refreshed_at        = now()
"""


def refresh_daily_pnl(day: dt.date) -> None:
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(REFRESH_DAILY_PNL_SQL, (day, day))
