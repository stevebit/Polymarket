"""Maker quoting strategy.

Heuristic: per event, pick the 1-3 buckets with highest model probability
mass that aren't dominated by neg-risk arbs. For each, post a buy quote a
half-spread below ``p_model`` and a sell quote a half-spread above
``p_model``, both clipped to the current book by 1 tick.

We deliberately quote the highest-prob bucket(s) per event because:
* That's where size is allowed under ``rewardsMaxSpread: 4.5¢`` (mid sits
  far from 0/1 there).
* That's where adverse selection is least toxic (small mispricings get
  corrected by professionals at fat tails first).

The maker module **does not** decide whether maker is preferred over taker —
that's the orchestrator's job, which compares the EV of both for each
bucket and picks whichever is higher.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .edge import (
    Action,
    BookLevel,
    Edge,
    FeeSchedule,
    maker_quote_yes_within_rewards,
)
from .sizing import CapsConfig, CapsState, SizedOrder, size_edge

# Maximum number of buckets per event we'll quote on (we want concentrated,
# not 11x duplicated quotes).
MAX_QUOTED_BUCKETS_PER_EVENT = 3
# Don't quote on buckets where p_model is too close to 0 or 1 (no rewards
# eligibility because mid is outside the 4.5¢ band).
P_QUOTE_MIN = 0.05
P_QUOTE_MAX = 0.95


@dataclass(frozen=True)
class MakerCandidate:
    buy_edge: Edge | None
    sell_edge: Edge | None
    buy_order: SizedOrder | None
    sell_order: SizedOrder | None
    target_date: dt.date
    event_slug: str
    bucket_label: str
    lead_days: int


def _select_quotable_buckets(
    bucket_probs: dict[str, float],
) -> list[tuple[str, float]]:
    """Top ``MAX_QUOTED_BUCKETS_PER_EVENT`` buckets by p_model, filtered to
    the quotable probability band."""
    items = [
        (k, p) for k, p in bucket_probs.items()
        if P_QUOTE_MIN <= p <= P_QUOTE_MAX
    ]
    items.sort(key=lambda kv: kv[1], reverse=True)
    return items[:MAX_QUOTED_BUCKETS_PER_EVENT]


def maker_for_bucket(
    *,
    p_yes: float,
    book: BookLevel,
    target_date: dt.date,
    today: dt.date,
    event_slug: str,
    bucket_label: str,
    caps: CapsConfig,
    state: CapsState = CapsState(),
    fees: FeeSchedule = FeeSchedule(),
    target_spread: float = 0.04,
    reward_per_share: float = 0.0,
) -> MakerCandidate | None:
    if not (P_QUOTE_MIN <= p_yes <= P_QUOTE_MAX):
        return None
    buy_edge, sell_edge = maker_quote_yes_within_rewards(
        p_yes,
        book,
        target_spread=target_spread,
        reward_per_share=reward_per_share,
        fees=fees,
    )

    buy_order = (
        size_edge(buy_edge, caps=caps, state=state)
        if buy_edge is not None and buy_edge.ev_per_share > 0
        else None
    )
    sell_order = (
        size_edge(sell_edge, caps=caps, state=state)
        if sell_edge is not None and sell_edge.ev_per_share > 0
        else None
    )
    if buy_order is None and sell_order is None:
        return None

    lead = max(0, (target_date - today).days)
    return MakerCandidate(
        buy_edge=buy_edge,
        sell_edge=sell_edge,
        buy_order=buy_order,
        sell_order=sell_order,
        target_date=target_date,
        event_slug=event_slug,
        bucket_label=bucket_label,
        lead_days=lead,
    )


def select_event_maker_quotes(
    bucket_probs: dict[str, float],
    books: dict[str, BookLevel],
    *,
    target_date: dt.date,
    today: dt.date,
    event_slug: str,
    caps: CapsConfig,
    state: CapsState = CapsState(),
    fees: FeeSchedule = FeeSchedule(),
    target_spread: float = 0.04,
    reward_per_share_by_bucket: dict[str, float] | None = None,
) -> list[MakerCandidate]:
    rps = reward_per_share_by_bucket or {}
    out: list[MakerCandidate] = []
    for label, p in _select_quotable_buckets(bucket_probs):
        book = books.get(label)
        if book is None:
            continue
        cand = maker_for_bucket(
            p_yes=p,
            book=book,
            target_date=target_date,
            today=today,
            event_slug=event_slug,
            bucket_label=label,
            caps=caps,
            state=state,
            fees=fees,
            target_spread=target_spread,
            reward_per_share=rps.get(label, 0.0),
        )
        if cand is not None:
            out.append(cand)
    return out


# (Action enum re-exported so callers can switch on action types from one place.)
__all__ = [
    "MAX_QUOTED_BUCKETS_PER_EVENT",
    "MakerCandidate",
    "maker_for_bucket",
    "select_event_maker_quotes",
    "Action",
]
