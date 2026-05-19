"""Bucket integration + calibration metrics.

The Polymarket daily-temperature events resolve from the integer-rounded
TMAX in F. For a Gaussian forecast ``X ~ N(mu, sigma)``:

    p(low_tail   <= n) = F(n + 0.5)
    p(interior [lo, hi]) = F(hi + 0.5) - F(lo - 0.5)
    p(high_tail  >= n) = 1 - F(n - 0.5)

We renormalise the bucket vector across each event so probabilities sum to 1
(small renormalisation absorbs tail mass beyond the union of buckets).

Predictive distributions
------------------------
Three predictive distribution families are supported, behind a single
``predict_bucket_probs`` interface:

* :class:`GaussianDist` — single Gaussian (used by M0, M1, EMOS-corrected sources).
* :class:`GaussianMixture` — equally- or custom-weighted mixture of Gaussians,
  used by M2 to blend per-source EMOS-corrected predictives.
* :class:`EmpiricalCDF` — direct samples (e.g. ensemble members), with the
  same continuity correction applied at integer-F bucket edges.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Protocol, Sequence

import numpy as np
from scipy.stats import norm


@dataclass(frozen=True)
class BucketBounds:
    label: str
    lo_f: float | None
    hi_f: float | None


class Distribution(Protocol):
    """Predictive distribution interface; bucket integration uses ``cdf``."""

    def cdf(self, x: float) -> float:  # noqa: D401
        """Cumulative density at ``x`` (degrees F)."""

    @property
    def mean(self) -> float:  # used for legacy ``predicted_max_f`` storage
        ...

    @property
    def std(self) -> float:  # used for legacy ``predicted_std_f`` storage
        ...


@dataclass(frozen=True)
class GaussianDist:
    mean_f: float
    std_f: float

    def cdf(self, x: float) -> float:
        if self.std_f <= 0:
            return 1.0 if x >= self.mean_f else 0.0
        return float(norm.cdf(x, loc=self.mean_f, scale=self.std_f))

    @property
    def mean(self) -> float:
        return float(self.mean_f)

    @property
    def std(self) -> float:
        return float(max(self.std_f, 0.0))


@dataclass(frozen=True)
class GaussianMixture:
    weights: tuple[float, ...]
    means: tuple[float, ...]
    stds: tuple[float, ...]

    def __post_init__(self) -> None:
        if not (len(self.weights) == len(self.means) == len(self.stds)):
            raise ValueError("weights, means, stds must have same length")
        if not self.weights:
            raise ValueError("at least one component required")

    def cdf(self, x: float) -> float:
        wsum = sum(self.weights)
        if wsum <= 0:
            return 0.0
        out = 0.0
        for w, mu, sd in zip(self.weights, self.means, self.stds):
            if sd <= 0:
                out += (w / wsum) * (1.0 if x >= mu else 0.0)
            else:
                out += (w / wsum) * float(norm.cdf(x, loc=mu, scale=sd))
        return out

    @property
    def mean(self) -> float:
        wsum = sum(self.weights)
        if wsum <= 0:
            return 0.0
        return sum(w * m for w, m in zip(self.weights, self.means)) / wsum

    @property
    def std(self) -> float:
        wsum = sum(self.weights)
        if wsum <= 0:
            return 0.0
        mu_bar = self.mean
        # Total variance = E[Var | k] + Var[E[. | k]]
        ev = sum(w * (sd**2) for w, sd in zip(self.weights, self.stds)) / wsum
        ve = sum(w * (m - mu_bar) ** 2 for w, m in zip(self.weights, self.means)) / wsum
        return float(math.sqrt(max(ev + ve, 0.0)))


@dataclass(frozen=True)
class EmpiricalCDF:
    """Empirical CDF over a fixed sample (e.g. ensemble members).

    ``samples`` is sorted ascending. CDF is the right-continuous step
    function: ``CDF(x) = #{s <= x} / N``.
    """

    samples: tuple[float, ...]

    def cdf(self, x: float) -> float:
        if not self.samples:
            return 0.0
        # binary search
        n = len(self.samples)
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) // 2
            if self.samples[mid] <= x:
                lo = mid + 1
            else:
                hi = mid
        return lo / n

    @property
    def mean(self) -> float:
        return float(np.mean(self.samples)) if self.samples else 0.0

    @property
    def std(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        return float(np.std(self.samples, ddof=1))


@dataclass(frozen=True)
class PercentileCDF:
    """Piecewise-linear CDF built from a fixed set of (probability, value)
    knots — e.g. NBM probabilistic bulletin's P10 / P25 / P50 / P75 / P90.

    ``quantiles`` and ``values`` are paired, sorted by ``quantiles``
    ascending. Outside the lowest/highest knot the CDF is clamped (we
    don't have tail information). Use only with bucket boundaries that
    fall inside the knot range; otherwise pair this with the model
    climatology floor.
    """

    quantiles: tuple[float, ...]
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.quantiles) != len(self.values):
            raise ValueError("quantiles and values must be the same length")
        if len(self.quantiles) < 2:
            raise ValueError("at least two knots required")
        if any(
            self.quantiles[i] >= self.quantiles[i + 1]
            for i in range(len(self.quantiles) - 1)
        ):
            raise ValueError("quantiles must be strictly increasing")

    def cdf(self, x: float) -> float:
        # Find the interval where ``self.values[i] <= x < self.values[i+1]``.
        vs = self.values
        qs = self.quantiles
        if x <= vs[0]:
            return qs[0] * (x >= vs[0] - 1e-9)
        if x >= vs[-1]:
            return qs[-1] + (1.0 - qs[-1]) * (x > vs[-1] + 1e-9)
        # Find the bracketing index via linear scan (n is small, e.g. 5).
        for i in range(len(vs) - 1):
            if vs[i] <= x <= vs[i + 1]:
                if vs[i + 1] == vs[i]:
                    return qs[i + 1]
                frac = (x - vs[i]) / (vs[i + 1] - vs[i])
                return float(qs[i] + frac * (qs[i + 1] - qs[i]))
        return float(qs[-1])

    @property
    def mean(self) -> float:
        # Approximate the mean by trapezoid integration of x dF.
        qs = self.quantiles
        vs = self.values
        m = 0.0
        for i in range(len(qs) - 1):
            dq = qs[i + 1] - qs[i]
            m += 0.5 * (vs[i] + vs[i + 1]) * dq
        # Tail mass below first knot and above last knot is treated as a
        # point mass at the knot value (consistent with `cdf` clamping).
        m += vs[0] * qs[0]
        m += vs[-1] * (1.0 - qs[-1])
        return float(m)

    @property
    def std(self) -> float:
        mu = self.mean
        qs = self.quantiles
        vs = self.values
        var = 0.0
        for i in range(len(qs) - 1):
            dq = qs[i + 1] - qs[i]
            var += 0.5 * ((vs[i] - mu) ** 2 + (vs[i + 1] - mu) ** 2) * dq
        var += (vs[0] - mu) ** 2 * qs[0]
        var += (vs[-1] - mu) ** 2 * (1.0 - qs[-1])
        return float(math.sqrt(max(var, 0.0)))


# ---------------------------------------------------------------------------
# Bucket integration
# ---------------------------------------------------------------------------


def _bucket_prob_from_cdf(bucket: BucketBounds, cdf) -> float:
    if bucket.lo_f is None and bucket.hi_f is not None:
        return float(cdf(bucket.hi_f + 0.5))
    if bucket.hi_f is None and bucket.lo_f is not None:
        return float(1.0 - cdf(bucket.lo_f - 0.5))
    if bucket.lo_f is not None and bucket.hi_f is not None:
        hi = float(cdf(bucket.hi_f + 0.5))
        lo = float(cdf(bucket.lo_f - 0.5))
        return max(0.0, hi - lo)
    return 0.0


def bucket_probability(
    bucket: BucketBounds,
    mean_f: float,
    std_f: float,
) -> float:
    """Backwards-compatible shim around :class:`GaussianDist`."""
    if std_f <= 0:
        v = round(mean_f)
        if bucket.lo_f is None and bucket.hi_f is not None:
            return 1.0 if v <= int(bucket.hi_f) else 0.0
        if bucket.hi_f is None and bucket.lo_f is not None:
            return 1.0 if v >= int(bucket.lo_f) else 0.0
        if bucket.lo_f is not None and bucket.hi_f is not None:
            return 1.0 if int(bucket.lo_f) <= v <= int(bucket.hi_f) else 0.0
        return 0.0
    return _bucket_prob_from_cdf(bucket, GaussianDist(mean_f, std_f).cdf)


def predict_bucket_probs(
    buckets: Sequence[BucketBounds],
    distribution: Distribution,
) -> dict[str, float]:
    """Distribution-agnostic bucket integration with renormalisation."""
    raw = {b.label: _bucket_prob_from_cdf(b, distribution.cdf) for b in buckets}
    s = sum(raw.values())
    if s <= 0:
        n = max(1, len(buckets))
        return {k: 1.0 / n for k in raw}
    return {k: v / s for k, v in raw.items()}


def event_probabilities(
    buckets: Sequence[BucketBounds],
    mean_f: float,
    std_f: float,
) -> dict[str, float]:
    """Backwards-compatible shim that builds a Gaussian and integrates."""
    return predict_bucket_probs(buckets, GaussianDist(mean_f, std_f))


def realised_bucket(
    buckets: Sequence[BucketBounds],
    observed_max_f: float,
) -> BucketBounds | None:
    # Half-up rounding (review §2.9): Python's built-in ``round()`` uses
    # banker's rounding, so e.g. ``round(70.5) == 70`` not 71. That maps
    # the boundary case to the wrong bucket once every ~100 events. Use
    # ``floor(x + 0.5)`` for unambiguous "round half up" semantics.
    v = int(math.floor(observed_max_f + 0.5))
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


def lead_stratified_crps_ablation(
    primary_preds: np.ndarray,
    neighbor_preds: np.ndarray | None,
    y: np.ndarray,
    leads: np.ndarray,
) -> dict[int, dict[str, float]]:
    """Compute lead-stratified CRPS for primary-only vs primary+neighbors ablation.

    Returns per-lead dict with 'primary_crps', 'neighbor_crps' (or None if no
    neighbor data), 'delta', 'n'. Used by calibration / neighbor-ablation report
    to quantify spatial context value on holdout (esp. leads 0-3 coastal/urban).
    """
    from .models.postprocess import _crps_gaussian  # local reuse of CRPS

    if neighbor_preds is None:
        neighbor_preds = primary_preds  # fallback for primary-only run
    res: dict[int, dict[str, float]] = {}
    for lead in sorted(np.unique(leads)):
        mask = leads == lead
        if mask.sum() < 5:
            continue
        p_crps = float(_crps_gaussian(primary_preds[mask], np.full_like(primary_preds[mask], 4.0), y[mask]).mean())
        n_crps = float(_crps_gaussian(neighbor_preds[mask], np.full_like(neighbor_preds[mask], 4.0), y[mask]).mean())
        res[int(lead)] = {
            "primary_crps": p_crps,
            "neighbor_crps": n_crps,
            "delta": n_crps - p_crps,
            "n": int(mask.sum()),
        }
    return res
