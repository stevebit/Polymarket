"""Bucket integration + calibration metrics.

The Polymarket daily-temperature events resolve from the integer-rounded
TMAX in F. For a Gaussian forecast ``X ~ N(mu, sigma)``:

    p(low_tail   <= n) = F(n + 0.5)
    p(interior [lo, hi]) = F(hi + 0.5) - F(lo - 0.5)
    p(high_tail  >= n) = 1 - F(n - 0.5)

We renormalise the bucket vector across each event so probabilities sum to 1
(small renormalisation absorbs tail mass beyond the union of buckets).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np
from scipy.stats import norm


@dataclass(frozen=True)
class BucketBounds:
    label: str
    lo_f: float | None
    hi_f: float | None


def bucket_probability(
    bucket: BucketBounds,
    mean_f: float,
    std_f: float,
) -> float:
    if std_f <= 0:
        # Degenerate forecast: hand back a hard 0/1 about ``mean_f``.
        v = round(mean_f)
        if bucket.lo_f is None and bucket.hi_f is not None:
            return 1.0 if v <= int(bucket.hi_f) else 0.0
        if bucket.hi_f is None and bucket.lo_f is not None:
            return 1.0 if v >= int(bucket.lo_f) else 0.0
        if bucket.lo_f is not None and bucket.hi_f is not None:
            return 1.0 if int(bucket.lo_f) <= v <= int(bucket.hi_f) else 0.0
        return 0.0

    if bucket.lo_f is None and bucket.hi_f is not None:
        return float(norm.cdf(bucket.hi_f + 0.5, loc=mean_f, scale=std_f))
    if bucket.hi_f is None and bucket.lo_f is not None:
        return float(1.0 - norm.cdf(bucket.lo_f - 0.5, loc=mean_f, scale=std_f))
    if bucket.lo_f is not None and bucket.hi_f is not None:
        hi = float(norm.cdf(bucket.hi_f + 0.5, loc=mean_f, scale=std_f))
        lo = float(norm.cdf(bucket.lo_f - 0.5, loc=mean_f, scale=std_f))
        return max(0.0, hi - lo)
    return 0.0


def event_probabilities(
    buckets: Sequence[BucketBounds],
    mean_f: float,
    std_f: float,
) -> dict[str, float]:
    raw = {b.label: bucket_probability(b, mean_f, std_f) for b in buckets}
    s = sum(raw.values())
    if s <= 0:
        # All zero (e.g. degenerate spike outside every bucket): fall back to
        # uniform so bookkeeping stays sane.
        n = max(1, len(buckets))
        return {k: 1.0 / n for k in raw}
    return {k: v / s for k, v in raw.items()}


def realised_bucket(
    buckets: Sequence[BucketBounds],
    observed_max_f: float,
) -> BucketBounds | None:
    v = int(round(observed_max_f))
    for b in buckets:
        if b.lo_f is None and b.hi_f is not None and v <= int(b.hi_f):
            return b
        if b.hi_f is None and b.lo_f is not None and v >= int(b.lo_f):
            return b
        if (
            b.lo_f is not None
            and b.hi_f is not None
            and int(b.lo_f) <= v <= int(b.hi_f)
        ):
            return b
    return None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def log_loss_one(prob: float, *, eps: float = 1e-12) -> float:
    p = min(max(prob, eps), 1.0 - eps)
    return -math.log(p)


def brier_one(probs: dict[str, float], realised_label: str) -> float:
    """Multi-class Brier score for a single event."""
    s = 0.0
    for k, p in probs.items():
        y = 1.0 if k == realised_label else 0.0
        s += (p - y) ** 2
    return s


@dataclass
class ReliabilityBin:
    bin_lo: float
    bin_hi: float
    count: int
    avg_pred_prob: float
    empirical_freq: float


def reliability_bins(
    pred_probs: Iterable[float],
    outcomes: Iterable[int],
    *,
    n_bins: int = 10,
) -> list[ReliabilityBin]:
    """For binary indicator pairs, group predictions into bins and compute
    average predicted probability and empirical frequency. Inputs are
    *flattened* across all (event, bucket) pairs."""
    p_arr = np.asarray(list(pred_probs), dtype=float)
    y_arr = np.asarray(list(outcomes), dtype=int)
    if p_arr.size == 0:
        return []
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    out: list[ReliabilityBin] = []
    for i in range(n_bins):
        lo, hi = float(edges[i]), float(edges[i + 1])
        if i == n_bins - 1:
            mask = (p_arr >= lo) & (p_arr <= hi)
        else:
            mask = (p_arr >= lo) & (p_arr < hi)
        n = int(mask.sum())
        if n == 0:
            out.append(ReliabilityBin(lo, hi, 0, float("nan"), float("nan")))
            continue
        out.append(
            ReliabilityBin(
                bin_lo=lo,
                bin_hi=hi,
                count=n,
                avg_pred_prob=float(p_arr[mask].mean()),
                empirical_freq=float(y_arr[mask].mean()),
            )
        )
    return out


def reliability_to_jsonable(bins: list[ReliabilityBin]) -> list[dict]:
    return [
        {
            "bin_lo": b.bin_lo,
            "bin_hi": b.bin_hi,
            "count": b.count,
            "avg_pred_prob": None if math.isnan(b.avg_pred_prob) else b.avg_pred_prob,
            "empirical_freq": None if math.isnan(b.empirical_freq) else b.empirical_freq,
        }
        for b in bins
    ]
