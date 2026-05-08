"""Signed order manager for the weather pipeline.

This is the **only** module under ``polymarket_weather`` that signs orders.
It wraps :func:`polymarket_manual.clients.make_trading_client` and exposes a
narrow surface — ``place_limit``, ``cancel``, ``replace``, ``list_open`` —
each gated by:

* ``WEATHER_AUTOMATION_ENABLED=1``: hard env switch. Anything else (unset,
  ``0``, ``false``) → calls return ``OrderManagerDisabled``.
* ``WEATHER_KILL_SWITCH``: any non-empty value disables placement and any
  call returns ``OrderManagerKilled``. Cancels still work so we can clean
  up open orders.
* Per-bucket / per-event / per-day / per-portfolio notional caps from
  :class:`polymarket_weather.strategy.sizing.CapsConfig`.

The orchestrator should call ``OrderManager.is_enabled()`` before doing
any work that depends on signed flow. When disabled, it falls back to the
paper-trading path (Phase 4b).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass

from ..strategy.edge import Action, FeeSchedule
from ..strategy.sizing import CapsConfig, SizedOrder
from . import state

log = logging.getLogger(__name__)

ENV_AUTOMATION = "WEATHER_AUTOMATION_ENABLED"
ENV_KILL_SWITCH = "WEATHER_KILL_SWITCH"


# ---------------------------------------------------------------------------
# Errors / sentinels
# ---------------------------------------------------------------------------


class OrderManagerError(RuntimeError):
    """Base error for the order manager."""


class OrderManagerDisabled(OrderManagerError):
    """Raised / returned when ``WEATHER_AUTOMATION_ENABLED`` is unset."""


class OrderManagerKilled(OrderManagerError):
    """Raised / returned when ``WEATHER_KILL_SWITCH`` is set."""


class CapBreach(OrderManagerError):
    """Raised when a placement would breach an active cap."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_automation_enabled() -> bool:
    val = os.environ.get(ENV_AUTOMATION, "").strip().lower()
    return val in ("1", "true", "yes", "on")


def is_kill_switch_set() -> bool:
    val = os.environ.get(ENV_KILL_SWITCH, "").strip()
    return bool(val)


def assert_can_place() -> None:
    if not is_automation_enabled():
        raise OrderManagerDisabled(
            f"{ENV_AUTOMATION} is not set; refusing to place real orders. "
            "Set the env var to '1' once Phase 4b paper trading has passed."
        )
    if is_kill_switch_set():
        raise OrderManagerKilled(
            f"{ENV_KILL_SWITCH} is set; refusing to place new orders. "
            "Unset the env var manually after investigating."
        )


# ---------------------------------------------------------------------------
# OrderManager
# ---------------------------------------------------------------------------


@dataclass
class PlacementContext:
    event_slug: str
    bucket_label: str
    target_date: dt.date
    sized: SizedOrder
    p_model: float
    model_id: str | None = None
    model_run_time: dt.datetime | None = None
    yes_token_id: str | None = None
    no_token_id: str | None = None


class OrderManager:
    """Lazily-wired wrapper around the v2 ClobClient. The client is only
    constructed if ``is_automation_enabled()``."""

    def __init__(
        self,
        *,
        caps: CapsConfig,
        fees: FeeSchedule = FeeSchedule(),
    ) -> None:
        self.caps = caps
        self.fees = fees
        self._client = None  # lazy

    # -- gated wiring -------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return is_automation_enabled() and not is_kill_switch_set()

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        assert_can_place()
        from polymarket_manual.clients import make_trading_client
        from polymarket_manual.config import load_settings

        self._client = make_trading_client(load_settings())
        return self._client

    # -- caps ---------------------------------------------------------------

    def _check_caps(
        self,
        *,
        event_slug: str,
        bucket_label: str,
        notional_usd: float,
    ) -> None:
        used_bucket = state.bucket_notional_used(event_slug, bucket_label)
        if used_bucket + notional_usd > self.caps.per_bucket_usd:
            raise CapBreach(
                f"per-bucket cap breach: used=${used_bucket:.2f} "
                f"+ ${notional_usd:.2f} > cap=${self.caps.per_bucket_usd:.2f}"
            )
        used_event = state.event_notional_used(event_slug)
        if used_event + notional_usd > self.caps.per_event_usd:
            raise CapBreach(
                f"per-event cap breach: used=${used_event:.2f} "
                f"+ ${notional_usd:.2f} > cap=${self.caps.per_event_usd:.2f}"
            )
        used_day = state.daily_notional_used(dt.date.today())
        if used_day + notional_usd > self.caps.per_day_usd:
            raise CapBreach(
                f"per-day cap breach: used=${used_day:.2f} "
                f"+ ${notional_usd:.2f} > cap=${self.caps.per_day_usd:.2f}"
            )
        used_portfolio = state.portfolio_notional_used()
        if used_portfolio + notional_usd > self.caps.per_portfolio_usd:
            raise CapBreach(
                f"per-portfolio cap breach: used=${used_portfolio:.2f} "
                f"+ ${notional_usd:.2f} > cap=${self.caps.per_portfolio_usd:.2f}"
            )

    # -- placement ----------------------------------------------------------

    def place_limit(self, ctx: PlacementContext) -> state.OrderRecord:
        """Place a single GTC limit order on the YES token. Returns the
        recorded :class:`state.OrderRecord`."""
        assert_can_place()

        side = ctx.sized.edge.action
        token_id = ctx.yes_token_id  # we always trade YES tokens
        if token_id is None:
            raise OrderManagerError(
                f"yes_token_id is required for {ctx.event_slug}/{ctx.bucket_label}"
            )

        self._check_caps(
            event_slug=ctx.event_slug,
            bucket_label=ctx.bucket_label,
            notional_usd=ctx.sized.notional_usd,
        )

        # Translate our action into the CLOB BUY/SELL primitive.
        buy_actions = {Action.MAKER_BUY, Action.TAKER_BUY}
        sell_actions = {Action.MAKER_SELL, Action.TAKER_SELL}
        if side in buy_actions:
            clob_side = "BUY"
        elif side in sell_actions:
            clob_side = "SELL"
        else:
            raise OrderManagerError(f"Unknown side {side!r}")

        from py_clob_client_v2.clob_types import OrderArgsV2, OrderType  # type: ignore
        from py_clob_client_v2.order_builder.constants import BUY, SELL  # type: ignore

        client = self._ensure_client()
        clob_const = BUY if clob_side == "BUY" else SELL
        client_order_id = state.new_client_order_id()

        order_args = OrderArgsV2(
            token_id=token_id,
            price=ctx.sized.edge.price,
            size=float(ctx.sized.shares),
            side=clob_const,
        )
        signed = client.create_order(order_args)
        # ``OrderType.GTC`` for resting limits; switch to FOK for taker-only
        # behaviour if we want stricter rejection rules.
        order_type = (
            OrderType.GTC
            if side in (Action.MAKER_BUY, Action.MAKER_SELL)
            else OrderType.FAK
        )
        resp = client.post_order(signed, order_type)
        if not isinstance(resp, dict):
            resp_dict = {"raw": str(resp)}
        else:
            resp_dict = resp
        order_id = (
            resp_dict.get("orderID")
            or resp_dict.get("orderId")
            or resp_dict.get("order_id")
            or client_order_id
        )
        status = (resp_dict.get("status") or "open").lower()
        record = state.OrderRecord(
            order_id=str(order_id),
            client_order_id=client_order_id,
            event_slug=ctx.event_slug,
            bucket_label=ctx.bucket_label,
            target_date=ctx.target_date,
            side=side.value,
            token_id=token_id,
            price=float(ctx.sized.edge.price),
            requested_shares=int(ctx.sized.shares),
            status=status,
            p_model_at_post=float(ctx.p_model),
            expected_value_usd=float(ctx.sized.expected_value_usd),
            model_id=ctx.model_id,
            model_run_time=ctx.model_run_time,
            notes=ctx.sized.notes,
            raw=resp_dict,
        )
        state.record_order(record)
        log.info(
            "placed %s %s %s @ %.3f x %d on %s/%s status=%s",
            order_type.name if hasattr(order_type, "name") else order_type,
            clob_side, side.value,
            float(ctx.sized.edge.price), int(ctx.sized.shares),
            ctx.event_slug, ctx.bucket_label, status,
        )
        return record

    def cancel(self, order_id: str) -> dict:
        """Cancel a single order by its exchange-assigned order_id. Cancels
        are allowed even when the kill switch is set so we can clean up."""
        if not is_automation_enabled():
            raise OrderManagerDisabled(
                f"{ENV_AUTOMATION} is not set; refusing to talk to CLOB."
            )
        client = self._ensure_client()
        resp = client.cancel_order(order_id)  # type: ignore[attr-defined]
        state.update_order_status(order_id, status="cancelled")
        return resp if isinstance(resp, dict) else {"raw": str(resp)}

    def replace(
        self,
        *,
        order_id: str,
        new_ctx: PlacementContext,
    ) -> state.OrderRecord:
        """Cancel the existing order then place a new one with the new ctx."""
        try:
            self.cancel(order_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("cancel during replace failed for %s: %s", order_id, exc)
        return self.place_limit(new_ctx)

    def list_open(self) -> list[dict]:
        """Live open orders from the CLOB. The orchestrator merges this with
        ``state.list_open_orders()`` to reconcile."""
        if not is_automation_enabled():
            return []
        client = self._ensure_client()
        try:
            raw = client.get_orders()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            log.warning("get_orders failed: %s", exc)
            return []
        if isinstance(raw, list):
            return raw
        if hasattr(raw, "__iter__"):
            return list(raw)
        return []
