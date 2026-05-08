"""Taker entry strategy.

Only fires when ``edge_per_dollar > MIN_EDGE_CENTS_PER_DOLLAR + adverse_selection_pad``.

The padding rises with lead time to discount the (still-developing) model
edge: at D+0 the obs-so-far feature has cut sigma, so we can be more
aggressive; at D+5+ the forecast is mostly NWP-only and the market may have
already digested similar information.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .edge import Edge, FeeSchedule, best_taker_edge, BucketBook
from .sizing import CapsConfig, CapsState, SizedOrder, size_edge

# Adverse-selection pad as a function of lead day.
ADVERSE_SELECTION_BY_LEAD_DAYS = {
    0: 0.000,    # nowcast: trust the obs-so-far
    1: 0.005,
    2: 0.010,
    3: 0.012,
    4: 0.014,
    5: 0.015,
    6: 0.015,
    7: 0.015,
}
DEFAULT_ADVERSE_SELECTION = 0.015


def adverse_selection_pad(lead_days: int) -> float:
    if lead_days < 0:
        lead_days = 0
    return ADVERSE_SELECTION_BY_LEAD_DAYS.get(lead_days, DEFAULT_ADVERSE_SELECTION)


@dataclass(frozen=True)
class TakerCandidate:
    edge: Edge
    sized: SizedOrder
    target_date: dt.date
    event_slug: str
    bucket_label: str
    lead_days: int


def best_taker_for_bucket(
    *,
    p_yes: float,
    book: BucketBook,
    target_date: dt.date,
    today: dt.date,
    event_slug: str,
    bucket_label: str,
    caps: CapsConfig,
    state: CapsState = CapsState(),
    fees: FeeSchedule = FeeSchedule(),
) -> TakerCandidate | None:
    """Return the best taker candidate for this bucket, or None if no edge
    survives the per-lead-day adverse-selection threshold."""
    edge = best_taker_edge(p_yes, book, fees=fees)
    if edge is None:
        return None
    lead = max(0, (target_date - today).days)
    pad = adverse_selection_pad(lead)
    # Edge per dollar must exceed pad + min_edge_per_dollar.
    edge_per_dollar = edge.ev_per_share / max(edge.price, 1e-6)
    if edge_per_dollar < pad + caps.min_edge_per_dollar:
        return None
    sized = size_edge(edge, caps=caps, state=state)
    if sized is None:
        return None
    return TakerCandidate(
        edge=edge,
        sized=sized,
        target_date=target_date,
        event_slug=event_slug,
        bucket_label=bucket_label,
        lead_days=lead,
    )
