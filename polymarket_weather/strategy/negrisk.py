"""Neg-risk projection and arbitrage detection.

Polymarket weather events are configured with ``enableNegRisk: true``: across
the 11 mutually-exclusive buckets per event, exactly one resolves YES, so
in fair pricing the YES probabilities sum to 1. The book often violates
this — sum of YES asks may be < 0.99 (overround opportunity for buyers) or
> 1.01 (small underround for sellers). This module:

1. Projects a model probability vector onto the simplex defined by the
   current best-bid layer of YES tokens (so we don't recommend buying YES at
   a price that's already higher than the best-bid neg-risk implied prob).
2. Flags risk-free arbitrages when the book violates neg-risk by more than
   a configurable tolerance.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Default tolerance: ignore arbs smaller than 1 cent on a $1 notional
# (covered by typical fees on the round trip).
DEFAULT_ARB_TOLERANCE = 0.01


@dataclass(frozen=True)
class BucketView:
    label: str
    p_model: float
    yes_bid: float | None
    yes_ask: float | None


@dataclass(frozen=True)
class NegRiskAnalysis:
    sum_p_model: float
    sum_yes_bids: float
    sum_yes_asks: float
    arb_buy_all_yes: float    # if you buy every YES at ask, +ve = profit per $1
    arb_sell_all_yes: float   # if you sell every YES at bid, +ve = profit per $1
    flagged_arb: bool


def analyse_event(
    views: list[BucketView],
    *,
    tolerance: float = DEFAULT_ARB_TOLERANCE,
) -> NegRiskAnalysis:
    sum_p = sum(v.p_model for v in views)
    sum_bids = sum(v.yes_bid for v in views if v.yes_bid is not None)
    sum_asks = sum(v.yes_ask for v in views if v.yes_ask is not None)

    # Buy-all arb: if sum of best asks < 1, you can buy every YES (one of
    # them must win) for less than $1 and collect $1 — risk-free.
    # Caller subtracts taker fee separately.
    arb_buy = 1.0 - sum_asks if sum_asks > 0 else 0.0
    # Sell-all arb: if sum of bids > 1, you can sell every YES at bid; one
    # of them costs $1 to settle, the rest are zero. Profit per $1 of
    # collected size.
    arb_sell = sum_bids - 1.0 if sum_bids > 0 else 0.0

    flagged = (arb_buy > tolerance) or (arb_sell > tolerance)
    return NegRiskAnalysis(
        sum_p_model=sum_p,
        sum_yes_bids=sum_bids,
        sum_yes_asks=sum_asks,
        arb_buy_all_yes=arb_buy,
        arb_sell_all_yes=arb_sell,
        flagged_arb=flagged,
    )


def project_to_simplex(p: np.ndarray) -> np.ndarray:
    """Euclidean projection of ``p`` onto the probability simplex.

    Wang & Carreira-Perpiñán, 2013.
    """
    n = len(p)
    if n == 0:
        return p
    u = np.sort(p)[::-1]
    cssv = np.cumsum(u)
    rho_candidates = u + (1.0 - cssv) / np.arange(1, n + 1)
    rho_idx = np.where(rho_candidates > 0)[0]
    if rho_idx.size == 0:
        out = np.full_like(p, 1.0 / n)
        return out
    rho = rho_idx[-1]
    lam = (1.0 - cssv[rho]) / (rho + 1)
    return np.maximum(p + lam, 0.0)


def coherent_model_probs(
    raw_probs: dict[str, float],
) -> dict[str, float]:
    """Take raw model bucket probs (which sum approximately to 1 already
    since :func:`predict_bucket_probs` renormalises), and project them onto
    the simplex to ensure exact sum = 1. Defensive against numerical drift."""
    if not raw_probs:
        return raw_probs
    labels = list(raw_probs.keys())
    arr = np.array([raw_probs[k] for k in labels], dtype=float)
    proj = project_to_simplex(arr)
    return {k: float(v) for k, v in zip(labels, proj)}
