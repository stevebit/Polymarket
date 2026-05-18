"""Per-model isotonic recalibration of bucket probabilities.

Plan Phase 6: when the like-for-like calibration shows a model's
reliability bins are systematically off-diagonal (>= 3 bins in the same
direction), an isotonic fit is the right tool — it is the minimum
assumption monotone calibrator and won't over-fit on tiny samples like a
neural calibrator might.

Storage: ``isotonic_calibration`` table from migration 006. Each row
holds the (sorted) ``x_knots`` and ``y_knots`` arrays that define the
isotonic step function: for an input probability ``p``, the calibrated
output is ``np.interp(p, x_knots, y_knots)`` clamped to ``[0, 1]``.

Decision rule (review §6, plan): only apply isotonic if there is robust
miscalibration. ``isotonic_recommended`` returns ``True`` when the
training-set ``bins_with_signed_gap >= 3`` and the signs are consistent.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from sklearn.isotonic import IsotonicRegression

from ..db import with_conn
from ..score import (
    BucketBounds,
    brier_one,
    log_loss_one,
    realised_bucket,
    reliability_bins,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IsotonicFit:
    model_id: str
    x_knots: tuple[float, ...]
    y_knots: tuple[float, ...]
    n_train: int
    train_brier: float
    train_logloss: float


# ---------------------------------------------------------------------------
# Data assembly
# ---------------------------------------------------------------------------


def _fetch_flattened_pairs(
    cur, model_id: str, lookback_days: int
) -> tuple[np.ndarray, np.ndarray]:
    """Flatten ``(predicted_prob, indicator)`` over resolved events.

    Uses the same DISTINCT-ON-latest-run_time pattern as the calibration
    SQL (review §2.3) so the fit is anchored to one bucket_probs snapshot
    per event/bucket.
    """
    cutoff = dt.date.today() - dt.timedelta(days=lookback_days)
    cur.execute(
        """
        WITH latest_bp AS (
            SELECT DISTINCT ON (bp.event_slug, bp.bucket_label)
                bp.event_slug,
                bp.bucket_label,
                bp.prob::float8 AS prob,
                bp.run_time
            FROM bucket_probs bp
            JOIN pm_events e2 ON e2.event_slug = bp.event_slug
            WHERE bp.model_id   = %s
              AND e2.target_date <= CURRENT_DATE
              AND e2.target_date >= %s
              AND bp.run_time   <= (e2.target_date::timestamp + interval '12 hours')
            ORDER BY bp.event_slug, bp.bucket_label, bp.run_time DESC
        )
        SELECT lb.event_slug,
               b.bucket_label,
               b.lo_f,
               b.hi_f,
               lb.prob,
               o.observed_max_f::float8
        FROM latest_bp lb
        JOIN pm_events  e ON e.event_slug = lb.event_slug
        JOIN pm_buckets b ON b.event_slug = lb.event_slug
                          AND b.bucket_label = lb.bucket_label
        JOIN observations o
                          ON o.station_id   = e.station_id
                         AND o.obs_date     = e.target_date
                         AND o.observed_max_f IS NOT NULL
                         AND o.finalized   = TRUE
        ORDER BY lb.event_slug, b.bucket_label
        """,
        (model_id, cutoff),
    )
    rows = cur.fetchall()
    if not rows:
        return np.array([]), np.array([])

    # Group by event to find the realised bucket then emit (prob, indicator).
    by_event: dict[str, dict] = {}
    for slug, label, lo, hi, prob, observed in rows:
        e = by_event.setdefault(slug, {"buckets": [], "probs": {}, "obs": float(observed)})
        e["buckets"].append(
            BucketBounds(
                label=label,
                lo_f=None if lo is None else float(lo),
                hi_f=None if hi is None else float(hi),
            )
        )
        e["probs"][label] = float(prob)

    xs: list[float] = []
    ys: list[int] = []
    for e in by_event.values():
        realised = realised_bucket(e["buckets"], e["obs"])
        if realised is None:
            continue
        for label, p in e["probs"].items():
            xs.append(p)
            ys.append(1 if label == realised.label else 0)
    return np.array(xs, dtype=float), np.array(ys, dtype=float)


def isotonic_recommended(
    xs: np.ndarray, ys: np.ndarray, *, min_bins: int = 3
) -> bool:
    """Apply isotonic only if the reliability has a consistent off-diagonal
    of at least ``min_bins`` bins in the same direction (review §6).
    """
    if xs.size < 200:
        return False
    bins = reliability_bins(xs.tolist(), ys.tolist())
    nonempty = [b for b in bins if b.count > 0]
    if len(nonempty) < min_bins:
        return False
    gaps = [b.empirical_freq - b.avg_pred_prob for b in nonempty if abs(b.empirical_freq - b.avg_pred_prob) > 0.03]
    if len(gaps) < min_bins:
        return False
    signs = {1 if g > 0 else -1 for g in gaps}
    return len(signs) == 1


# ---------------------------------------------------------------------------
# Fit / apply
# ---------------------------------------------------------------------------


def fit_isotonic(
    model_id: str, lookback_days: int = 90
) -> IsotonicFit | None:
    """Fit ``IsotonicRegression`` over the flattened ``(prob, indicator)``
    pairs for ``model_id``. Returns ``None`` if there isn't enough data."""
    with with_conn() as conn, conn.cursor() as cur:
        xs, ys = _fetch_flattened_pairs(cur, model_id, lookback_days)
    if xs.size == 0:
        log.info("fit_isotonic: no data for model_id=%s", model_id)
        return None
    if xs.size < 200:
        log.info(
            "fit_isotonic: too few pairs (%d < 200) for stable isotonic fit",
            xs.size,
        )
        return None

    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(xs, ys)
    # ``IsotonicRegression.predict`` is what we'll use at apply time, but we
    # persist only the knot pairs so the runtime doesn't need sklearn.
    x_knots = np.array(iso.X_thresholds_, dtype=float)
    y_knots = np.array(iso.y_thresholds_, dtype=float)
    # Score on the training set so the saved row carries something useful.
    p_cal = np.clip(np.interp(xs, x_knots, y_knots), 0.0, 1.0)
    # Brier (mean of (p - y)^2).
    train_brier = float(((p_cal - ys) ** 2).mean())
    # Log-loss with clipping to avoid -inf.
    p_clip = np.clip(p_cal, 1e-9, 1 - 1e-9)
    train_logloss = float(-(ys * np.log(p_clip) + (1 - ys) * np.log(1 - p_clip)).mean())

    fit = IsotonicFit(
        model_id=model_id,
        x_knots=tuple(float(x) for x in x_knots),
        y_knots=tuple(float(y) for y in y_knots),
        n_train=int(xs.size),
        train_brier=train_brier,
        train_logloss=train_logloss,
    )
    persist_isotonic_fit(fit)
    log.info(
        "fit_isotonic %s n=%d knots=%d train_brier=%.4f train_ll=%.4f",
        model_id, fit.n_train, len(fit.x_knots), fit.train_brier, fit.train_logloss,
    )
    return fit


PERSIST_SQL = """
INSERT INTO isotonic_calibration
    (model_id, x_knots, y_knots, n_train, train_brier, train_logloss, fit_at)
VALUES (%s, %s, %s, %s, %s, %s, now())
"""


def persist_isotonic_fit(fit: IsotonicFit) -> None:
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            PERSIST_SQL,
            (
                fit.model_id,
                list(fit.x_knots),
                list(fit.y_knots),
                fit.n_train,
                fit.train_brier,
                fit.train_logloss,
            ),
        )


_FIT_CACHE: dict[str, IsotonicFit | None] = {}
_FIT_CACHE_LOADED: set[str] = set()


def cached_isotonic_fit(model_id: str) -> IsotonicFit | None:
    """Lightweight per-process cache for :func:`latest_isotonic_fit`.

    The fit changes only when ``fit_isotonic`` is re-run and is consulted on
    every event during a tick. Caching avoids an extra DB round-trip per
    event without introducing staleness within a single tick. Call
    :func:`invalidate_isotonic_cache` after a re-fit if you want freshness
    inside one long-running process.
    """
    if model_id in _FIT_CACHE_LOADED:
        return _FIT_CACHE.get(model_id)
    try:
        fit = latest_isotonic_fit(model_id)
    except Exception as exc:  # noqa: BLE001
        log.info("Isotonic fit lookup failed for %s: %s", model_id, exc)
        fit = None
    _FIT_CACHE[model_id] = fit
    _FIT_CACHE_LOADED.add(model_id)
    return fit


def invalidate_isotonic_cache(model_id: str | None = None) -> None:
    """Drop cached fits so the next ``cached_isotonic_fit`` call re-reads
    the DB. Pass ``None`` to clear all entries."""
    if model_id is None:
        _FIT_CACHE.clear()
        _FIT_CACHE_LOADED.clear()
    else:
        _FIT_CACHE.pop(model_id, None)
        _FIT_CACHE_LOADED.discard(model_id)


def latest_isotonic_fit(model_id: str) -> IsotonicFit | None:
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT x_knots, y_knots, n_train, train_brier, train_logloss
            FROM isotonic_calibration
            WHERE model_id = %s
            ORDER BY fit_at DESC
            LIMIT 1
            """,
            (model_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    x_knots, y_knots, n_train, train_brier, train_logloss = row
    return IsotonicFit(
        model_id=model_id,
        x_knots=tuple(float(x) for x in x_knots),
        y_knots=tuple(float(y) for y in y_knots),
        n_train=int(n_train),
        train_brier=float(train_brier) if train_brier is not None else float("nan"),
        train_logloss=float(train_logloss) if train_logloss is not None else float("nan"),
    )


def apply_isotonic(fit: IsotonicFit, probs: dict[str, float]) -> dict[str, float]:
    """Apply ``fit`` to a single bucket-probability vector.

    Each entry is calibrated individually with the per-model isotonic
    transform; the result is then renormalised across labels so it still
    forms a valid distribution (the per-entry transform is not
    sum-preserving).
    """
    if not probs:
        return probs
    labels = list(probs.keys())
    xs = np.array([probs[k] for k in labels], dtype=float)
    cal = np.clip(np.interp(xs, fit.x_knots, fit.y_knots), 0.0, 1.0)
    s = float(cal.sum())
    if s <= 0:
        n = max(1, len(cal))
        return {k: 1.0 / n for k in labels}
    cal = cal / s
    return {k: float(v) for k, v in zip(labels, cal)}
