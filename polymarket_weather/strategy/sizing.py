"""Fractional Kelly sizing with hard caps.

Inputs:

* ``edge.ev_per_share`` — expected dollar EV per share after fees.
* ``edge.price`` — quoted price (your cost basis per share for buys, your
  proceeds per share for sells).
* ``edge.p_model`` — model probability for the side we'd take.
* ``CapsConfig`` — per-bucket / per-event / per-day / per-portfolio dollar caps.

Output: ``SizedOrder`` with a recommended share count, dollar notional,
expected dollar EV, and the binding cap (if any).

Kelly formula for a binary bet at price ``p_market`` on a YES with model prob
``p_model``:

    f* = (p_model - p_market) / (1 - p_market)              [for buys]
    f* = ((1 - p_model) - (1 - p_market)) / p_market        [for sells, equivalent]

We then scale by ``KELLY_FRACTION`` (default 0.25) and clamp to caps.

For maker quotes, ``ev_per_share`` already includes fill probability, so we
size based on the fill-probability-discounted EV but quote the un-discounted
share count (otherwise we'd never fill in size).
"""

from __future__ import annotations

from dataclasses import dataclass

from .edge import Action, Edge

# Defaults appropriate for a tiny ($100 - $1000) bankroll.
DEFAULT_KELLY_FRACTION = 0.25
DEFAULT_MIN_EDGE_PER_DOLLAR = 0.02   # 2 cents per $1 notional after fees
DEFAULT_MIN_ORDER_SHARES = 5         # Polymarket CLOB enforced
DEFAULT_TICK_SIZE = 0.01


@dataclass(frozen=True)
class CapsConfig:
    bankroll_usd: float
    per_bucket_usd: float
    per_event_usd: float
    per_day_usd: float
    per_portfolio_usd: float
    kelly_fraction: float = DEFAULT_KELLY_FRACTION
    min_edge_per_dollar: float = DEFAULT_MIN_EDGE_PER_DOLLAR
    min_order_shares: int = DEFAULT_MIN_ORDER_SHARES


@dataclass(frozen=True)
class CapsState:
    """Live tally of how much room is left under each cap."""

    used_per_bucket: float = 0.0
    used_per_event: float = 0.0
    used_per_day: float = 0.0
    used_per_portfolio: float = 0.0


@dataclass(frozen=True)
class SizedOrder:
    edge: Edge
    shares: int
    notional_usd: float
    expected_value_usd: float
    binding_cap: str
    notes: str = ""


# ---------------------------------------------------------------------------
# Fractional-Kelly fraction
# ---------------------------------------------------------------------------


def _kelly_fraction_buy(p_model: float, price: float) -> float:
    """Standard binary-bet Kelly fraction for buying a YES at ``price``."""
    if price <= 0 or price >= 1:
        return 0.0
    return max(0.0, (p_model - price) / (1.0 - price))


def _kelly_fraction_sell(p_model: float, price: float) -> float:
    """Selling YES at ``price`` is equivalent to buying NO at ``1 - price``
    with model NO probability ``1 - p_model``."""
    return _kelly_fraction_buy(1.0 - p_model, 1.0 - price)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def size_edge(
    edge: Edge,
    *,
    caps: CapsConfig,
    state: CapsState = CapsState(),
    tick_size: float = DEFAULT_TICK_SIZE,
) -> SizedOrder | None:
    """Return a sized order, or None if edge is too small / caps exhausted."""
    # Filter by minimum-edge threshold first.
    if edge.ev_per_share <= 0:
        return None
    if edge.price <= 0 or edge.price >= 1:
        return None
    edge_per_dollar = edge.ev_per_share / edge.price
    if edge_per_dollar < caps.min_edge_per_dollar:
        return None

    if edge.action in (Action.TAKER_BUY, Action.MAKER_BUY):
        f_star = _kelly_fraction_buy(edge.p_model, edge.price)
    else:
        f_star = _kelly_fraction_sell(edge.p_model, edge.price)
    if f_star <= 0:
        return None

    target_dollars = caps.kelly_fraction * f_star * caps.bankroll_usd

    # Apply caps in increasing order of restrictiveness.
    binding = "kelly"
    if target_dollars > caps.per_bucket_usd - state.used_per_bucket:
        target_dollars = max(0.0, caps.per_bucket_usd - state.used_per_bucket)
        binding = "per_bucket"
    if target_dollars > caps.per_event_usd - state.used_per_event:
        target_dollars = max(0.0, caps.per_event_usd - state.used_per_event)
        binding = "per_event"
    if target_dollars > caps.per_day_usd - state.used_per_day:
        target_dollars = max(0.0, caps.per_day_usd - state.used_per_day)
        binding = "per_day"
    if target_dollars > caps.per_portfolio_usd - state.used_per_portfolio:
        target_dollars = max(
            0.0, caps.per_portfolio_usd - state.used_per_portfolio
        )
        binding = "per_portfolio"

    shares = int(target_dollars // edge.price)
    if shares < caps.min_order_shares:
        return None

    notional = shares * edge.price
    expected_value = shares * edge.ev_per_share
    return SizedOrder(
        edge=edge,
        shares=shares,
        notional_usd=notional,
        expected_value_usd=expected_value,
        binding_cap=binding,
        notes=f"f*={f_star:.3f} kelly_frac={caps.kelly_fraction:.2f}",
    )


# Tiny-bankroll preset — matches the Phase 6 caps in the plan.
def tiny_bankroll_caps(bankroll_usd: float = 500.0) -> CapsConfig:
    return CapsConfig(
        bankroll_usd=bankroll_usd,
        per_bucket_usd=5.0,
        per_event_usd=20.0,
        per_day_usd=100.0,
        per_portfolio_usd=min(bankroll_usd, 500.0),
        kelly_fraction=DEFAULT_KELLY_FRACTION,
        min_edge_per_dollar=DEFAULT_MIN_EDGE_PER_DOLLAR,
        min_order_shares=DEFAULT_MIN_ORDER_SHARES,
    )
