"""Fee-aware edge calculation.

Polymarket weather markets carry the following structure (per Gamma's
``feeSchedule`` field on each sub-market):

* ``rate``: 0.05 (5% **base rate** for weather category)
* ``takerOnly``: true (makers do not pay fees)
* ``rebateRate``: 0.25 (interpreted as a maker-side rebate fraction tied to
  the liquidity-rewards program; Polymarket's exact accounting is verified
  in Phase 6 by a small live trade)

**Fee formula** (CLOB v2, symmetric):

    fee_per_share = rate * p * (1 - p)

where ``p`` is the trade price in [0, 1]. The fee peaks at $0.0125/share at
``p=0.5`` and **decays to zero at both tails**. Earlier versions of this
module used a linear ``fee = rate * p`` approximation that overstates fees
by up to 20x at the high-price tail. See ``fee_per_share`` below.

Plus the per-market liquidity rewards via ``clobRewards``:

* ``rewardsMaxSpread``: 0.045 (4.5 cents — quotes within this of the mid
  qualify for daily USDC rewards)
* ``rewardsMinSize``: 50 shares (orders below this don't qualify)
* ``rewardsDailyRate``: USDC paid daily across qualifying liquidity at that
  market.

This module is read-only math: it consumes raw probabilities + book + fee
parameters and returns ``Edge`` records that downstream sizing/strategy
modules turn into orders. We never assume anything about the trader's
inventory here — that's the sizing module's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# Defaults reflect today's Polymarket weather schedule. They live in code,
# not env, because changing them silently is dangerous; per-call overrides
# are easy.
DEFAULT_TAKER_FEE = 0.05
DEFAULT_MAKER_FEE = 0.0
DEFAULT_REWARDS_MAX_SPREAD = 0.045
DEFAULT_REWARDS_MIN_SIZE = 50.0


class Side(str, Enum):
    YES = "yes"
    NO = "no"


class Action(str, Enum):
    TAKER_BUY = "taker_buy"
    TAKER_SELL = "taker_sell"
    MAKER_BUY = "maker_buy"
    MAKER_SELL = "maker_sell"


@dataclass(frozen=True)
class FeeSchedule:
    taker_fee: float = DEFAULT_TAKER_FEE
    maker_fee: float = DEFAULT_MAKER_FEE
    rewards_max_spread: float = DEFAULT_REWARDS_MAX_SPREAD
    rewards_min_size: float = DEFAULT_REWARDS_MIN_SIZE


def fee_per_share(price: float, rate: float) -> float:
    """Polymarket CLOB v2 symmetric fee.

    ``fee = rate * p * (1 - p)`` per share, where ``p`` is the trade price.
    Returns 0 outside the valid ``(0, 1)`` price range so callers can chain
    without an extra guard.

    Examples (rate=0.05):
        p=0.10 -> 0.0045 ; p=0.30 -> 0.0105 ; p=0.50 -> 0.0125
        p=0.70 -> 0.0105 ; p=0.90 -> 0.0045 ; p=0.95 -> 0.002375
    """
    if price <= 0.0 or price >= 1.0 or rate <= 0.0:
        return 0.0
    return rate * price * (1.0 - price)


@dataclass(frozen=True)
class BookLevel:
    """Top-of-book snapshot for a single token."""

    best_bid: float | None
    best_ask: float | None
    bid_size: float = 0.0
    ask_size: float = 0.0
    mid: float | None = None

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


@dataclass(frozen=True)
class Edge:
    """A single primitive trade idea.

    ``ev_per_share`` is the expected dollar return per 1 share on the YES
    token (or NO token) after fees. Positive means we expect to make money.
    """

    side: Side
    action: Action
    price: float            # quoted price for this idea (the limit we'd post / take)
    p_model: float          # model's probability of this side winning
    ev_per_share: float
    edge_per_share: float   # gross EV (no fees), used for diagnostics
    fee_per_share: float
    notes: str = ""


# ---------------------------------------------------------------------------
# Taker EV
# ---------------------------------------------------------------------------


def taker_buy_yes_edge(
    p_yes: float, ask_yes: float, *, fees: FeeSchedule = FeeSchedule()
) -> Edge | None:
    if ask_yes is None or ask_yes <= 0 or ask_yes >= 1:
        return None
    fee = fee_per_share(ask_yes, fees.taker_fee)
    gross = p_yes * 1.0 - ask_yes
    ev = gross - fee
    return Edge(
        side=Side.YES,
        action=Action.TAKER_BUY,
        price=ask_yes,
        p_model=p_yes,
        ev_per_share=ev,
        edge_per_share=gross,
        fee_per_share=fee,
        notes=f"taker_buy yes @ {ask_yes:.3f}",
    )


def taker_sell_yes_edge(
    p_yes: float, bid_yes: float, *, fees: FeeSchedule = FeeSchedule()
) -> Edge | None:
    """Sell a YES we hold (or get short via taker fill on the bid).

    Cash flow: receive ``bid_yes`` per share now, pay
    ``rate * bid_yes * (1 - bid_yes)`` taker fee, give up ``p_yes`` of
    expected dollar (since the YES would have paid 1 if it won).
    """
    if bid_yes is None or bid_yes <= 0 or bid_yes >= 1:
        return None
    fee = fee_per_share(bid_yes, fees.taker_fee)
    gross = bid_yes - p_yes
    ev = gross - fee
    return Edge(
        side=Side.YES,
        action=Action.TAKER_SELL,
        price=bid_yes,
        p_model=p_yes,
        ev_per_share=ev,
        edge_per_share=gross,
        fee_per_share=fee,
        notes=f"taker_sell yes @ {bid_yes:.3f}",
    )


def taker_buy_no_edge(
    p_yes: float, ask_no: float, *, fees: FeeSchedule = FeeSchedule()
) -> Edge | None:
    if ask_no is None or ask_no <= 0 or ask_no >= 1:
        return None
    fee = fee_per_share(ask_no, fees.taker_fee)
    p_no = 1.0 - p_yes
    gross = p_no - ask_no
    ev = gross - fee
    return Edge(
        side=Side.NO,
        action=Action.TAKER_BUY,
        price=ask_no,
        p_model=p_no,
        ev_per_share=ev,
        edge_per_share=gross,
        fee_per_share=fee,
        notes=f"taker_buy no @ {ask_no:.3f}",
    )


# ---------------------------------------------------------------------------
# Maker EV
# ---------------------------------------------------------------------------


def maker_quote_yes_edge(
    p_yes: float,
    quote_price: float,
    *,
    side: Action,
    fill_prob: float = 1.0,
    reward_per_share: float = 0.0,
    fees: FeeSchedule = FeeSchedule(),
) -> Edge | None:
    """Maker BUY at ``quote_price`` (i.e. post a bid). EV per posted share if
    filled = ``p_yes - quote_price + reward``. We multiply by ``fill_prob``
    because for a maker the "size" is shares **posted**, not shares filled.
    """
    if quote_price <= 0 or quote_price >= 1:
        return None
    if side == Action.MAKER_BUY:
        gross_if_filled = p_yes - quote_price
        scaled_gross = gross_if_filled * fill_prob
    elif side == Action.MAKER_SELL:
        gross_if_filled = quote_price - p_yes
        scaled_gross = gross_if_filled * fill_prob
    else:
        return None

    fee = fee_per_share(quote_price, fees.maker_fee)
    ev = scaled_gross + reward_per_share - fee
    return Edge(
        side=Side.YES,
        action=side,
        price=quote_price,
        p_model=p_yes,
        ev_per_share=ev,
        edge_per_share=scaled_gross,
        fee_per_share=fee,
        notes=(
            f"maker_{side.value} yes @ {quote_price:.3f} "
            f"fill_p={fill_prob:.2f} reward={reward_per_share:.4f}"
        ),
    )


# ---------------------------------------------------------------------------
# Bucket-level aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BucketBook:
    """Both YES and NO top-of-book for a single bucket. Use NO when YES
    side is illiquid; with neg-risk markets, NO is ``1 - YES`` plus the
    independent NO order book."""

    yes: BookLevel
    no: BookLevel | None = None


def best_taker_edge(
    p_yes: float,
    book: BucketBook,
    *,
    fees: FeeSchedule = FeeSchedule(),
) -> Edge | None:
    """Across the four primitive taker actions, return the most positive."""
    candidates: list[Edge] = []
    e = taker_buy_yes_edge(p_yes, book.yes.best_ask, fees=fees)
    if e is not None:
        candidates.append(e)
    e = taker_sell_yes_edge(p_yes, book.yes.best_bid, fees=fees)
    if e is not None:
        candidates.append(e)
    if book.no is not None:
        e = taker_buy_no_edge(p_yes, book.no.best_ask, fees=fees)
        if e is not None:
            candidates.append(e)
    candidates = [c for c in candidates if c.ev_per_share > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.ev_per_share)


def fill_prob_estimator(
    distance_from_mid: float | None,
    lead_days: int | None,
    station: str | None,
) -> float:
    """Maker fill probability.

    Consults :func:`polymarket_weather.models.maker_fill.fill_prob` for
    the learned logistic curve when one has been persisted in
    ``maker_fill_coefs`` (Phase 7). Falls back to a conservative 0.10
    default when no fit exists yet — significantly more pessimistic than
    the old hard-coded 0.5 — so EV is not inflated by an optimistic fill
    rate.
    """
    _ = station  # station-conditional curves are a Phase 7+ extension
    try:
        # Local import keeps the strategy module DB-free at import time.
        from ..models.maker_fill import fill_prob as _learned

        p = _learned(distance_from_mid, lead_days)
        if p is not None:
            # Conservative floor: never trust the curve below 5%.
            return max(0.05, min(0.95, float(p)))
    except Exception:  # noqa: BLE001
        pass
    return 0.10


def maker_quote_yes_within_rewards(
    p_yes: float,
    book: BookLevel,
    *,
    target_spread: float = 0.04,
    reward_per_share: float = 0.0,
    fees: FeeSchedule = FeeSchedule(),
    min_fill_prob: float = 0.10,
    min_edge: float = 0.0,
    lead_days: int | None = None,
    station: str | None = None,
) -> tuple[Edge | None, Edge | None]:
    """Pick a buy quote at ``p_model - target_spread/2`` and a sell quote at
    ``p_model + target_spread/2``, clipped to the current book by 1 tick.

    Returns ``(buy_edge, sell_edge)`` with EV per posted share.

    **Direction guard (review §2.5):** the buy side is suppressed when the
    model doesn't think YES is undervalued (``p_yes <= mid + min_edge``)
    and the sell side is suppressed when YES isn't overvalued
    (``p_yes >= mid - min_edge``). Without this, even a perfectly fair
    model posts both quotes whenever the spread is wider than the rewards
    band, which is wrong on direction.

    Fill probability comes from :func:`fill_prob_estimator` (currently a
    conservative 0.10 default; Phase 7 swaps in a learned curve).
    """
    if book.best_bid is None and book.best_ask is None:
        return None, None
    target_buy = p_yes - target_spread / 2.0
    target_sell = p_yes + target_spread / 2.0
    if book.best_ask is not None:
        target_buy = min(target_buy, max(0.01, book.best_ask - 0.01))
    if book.best_bid is not None:
        target_sell = max(target_sell, min(0.99, book.best_bid + 0.01))
    target_buy = max(0.01, min(0.99, round(target_buy, 2)))
    target_sell = max(0.01, min(0.99, round(target_sell, 2)))

    # Quotes that hit the reward band: |quote - mid| <= rewards_max_spread / 2
    mid = book.mid
    if mid is None and book.best_bid is not None and book.best_ask is not None:
        mid = (book.best_bid + book.best_ask) / 2.0

    # Direction guard: suppress quotes the model doesn't actually support.
    # Asymmetric example: p_model=0.20, mid=0.30. We should NOT post a buy
    # at 0.18 (we'd be paying to buy something the model says is below mid).
    # We SHOULD consider posting a sell at 0.32 (model says it's overpriced).
    allow_buy = mid is None or p_yes > mid + min_edge
    allow_sell = mid is None or p_yes < mid - min_edge

    fill_buy = max(
        min_fill_prob,
        fill_prob_estimator(
            None if mid is None else abs(target_buy - mid), lead_days, station,
        ),
    )
    fill_sell = max(
        min_fill_prob,
        fill_prob_estimator(
            None if mid is None else abs(target_sell - mid), lead_days, station,
        ),
    )
    buy_reward = sell_reward = 0.0
    if mid is not None and reward_per_share > 0:
        if abs(target_buy - mid) <= fees.rewards_max_spread:
            buy_reward = reward_per_share
        if abs(target_sell - mid) <= fees.rewards_max_spread:
            sell_reward = reward_per_share

    buy_edge = (
        maker_quote_yes_edge(
            p_yes,
            target_buy,
            side=Action.MAKER_BUY,
            fill_prob=fill_buy,
            reward_per_share=buy_reward,
            fees=fees,
        )
        if allow_buy
        else None
    )
    sell_edge = (
        maker_quote_yes_edge(
            p_yes,
            target_sell,
            side=Action.MAKER_SELL,
            fill_prob=fill_sell,
            reward_per_share=sell_reward,
            fees=fees,
        )
        if allow_sell
        else None
    )
    return buy_edge, sell_edge
