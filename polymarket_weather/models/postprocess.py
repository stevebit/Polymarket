"""EMOS / DRN postprocessing of raw forecast sources.

EMOS (Ensemble Model Output Statistics, Gneiting et al. 2005) for daily TMAX:

    mu(forecast)    = a + b * raw_forecast
    sigma(spread)   = sqrt(c + d * spread^2)        (positive c, d)

Where ``spread`` is the ensemble standard deviation when available, else a
constant. We fit per ``(station, source, lead_day)`` so each source can have
its own bias correction and uncertainty calibration.

Coefficients are persisted in ``postprocess_coefs`` (added in migration 002)
keyed by ``(model_id, station, source, lead_day, fit_at)``. The most recent
``fit_at`` is consumed at predict time.

The actual fit minimises CRPS analytically for Gaussian predictive
distributions. CRPS for ``X ~ N(mu, sigma)`` against observation y is:

    CRPS = sigma * [ z * (2 * Phi(z) - 1) + 2 * phi(z) - 1/sqrt(pi) ]
    z    = (y - mu) / sigma

We use scipy.optimize.minimize over (a, b, log_c, log_d) on the training set.
For deterministic sources (no spread), ``d`` collapses and we fit only
``(a, b, sigma_const)``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from scipy.optimize import minimize
from scipy.stats import norm

from ..db import station_id_by_slug, with_conn

log = logging.getLogger(__name__)

EMOS_MODEL_ID = "emos:v1"

# Don't fit if we have fewer than this many training pairs for a (station,
# source, lead) bucket; fall back to identity (no correction) instead.
MIN_TRAIN_PAIRS = 30
# Don't let predictive sigma collapse below this many degrees F.
MIN_SIGMA_F = 1.0
DEFAULT_SIGMA_F = 4.0


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------


def _crps_gaussian(mu: np.ndarray, sigma: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Closed-form CRPS for a Gaussian predictive distribution per row."""
    sigma = np.maximum(sigma, 1e-6)
    z = (y - mu) / sigma
    return sigma * (z * (2.0 * norm.cdf(z) - 1.0) + 2.0 * norm.pdf(z) - 1.0 / math.sqrt(math.pi))


def _emos_negloglik_with_spread(
    params: np.ndarray,
    raw: np.ndarray,
    spread: np.ndarray,
    y: np.ndarray,
) -> float:
    a, b, log_c, log_d = params
    c = math.exp(log_c)
    d = math.exp(log_d)
    mu = a + b * raw
    var = c + d * spread**2
    sigma = np.sqrt(np.maximum(var, 1e-6))
    # We minimise mean CRPS; equivalent training objective.
    return float(_crps_gaussian(mu, sigma, y).mean())


def _emos_negloglik_no_spread(
    params: np.ndarray,
    raw: np.ndarray,
    y: np.ndarray,
) -> float:
    a, b, log_sigma = params
    sigma = math.exp(log_sigma)
    mu = a + b * raw
    sigma_arr = np.full_like(mu, sigma)
    return float(_crps_gaussian(mu, sigma_arr, y).mean())


@dataclass
class EmosFit:
    a: float
    b: float
    c: float | None
    d: float | None
    sigma_const: float | None
    n_train: int
    train_crps: float
    train_rmse: float


def fit_emos(
    raw: np.ndarray,
    y: np.ndarray,
    *,
    spread: np.ndarray | None = None,
) -> EmosFit | None:
    """Fit EMOS for a single (station, source, lead) pair. Returns None if
    too few training pairs."""
    if raw.size < MIN_TRAIN_PAIRS:
        return None
    if spread is not None and (
        spread.size != raw.size or not np.any(np.isfinite(spread))
    ):
        spread = None

    # OLS init for (a, b)
    X = np.column_stack([np.ones_like(raw), raw])
    coef_init, *_ = np.linalg.lstsq(X, y, rcond=None)
    a0, b0 = float(coef_init[0]), float(coef_init[1])

    if spread is None:
        residuals = y - (a0 + b0 * raw)
        sigma0 = max(MIN_SIGMA_F, float(residuals.std(ddof=1)))
        x0 = np.array([a0, b0, math.log(sigma0)])
        res = minimize(
            _emos_negloglik_no_spread,
            x0,
            args=(raw, y),
            method="Nelder-Mead",
            options={"maxiter": 500, "xatol": 1e-3, "fatol": 1e-4},
        )
        a, b, log_sigma = res.x
        sigma_const = max(MIN_SIGMA_F, math.exp(log_sigma))
        mu_train = a + b * raw
        sigma_train = np.full_like(mu_train, sigma_const)
        return EmosFit(
            a=float(a),
            b=float(b),
            c=None,
            d=None,
            sigma_const=float(sigma_const),
            n_train=int(raw.size),
            train_crps=float(_crps_gaussian(mu_train, sigma_train, y).mean()),
            train_rmse=float(np.sqrt(((mu_train - y) ** 2).mean())),
        )

    residuals = y - (a0 + b0 * raw)
    var0 = max(MIN_SIGMA_F**2, float(residuals.var(ddof=1)))
    x0 = np.array([a0, b0, math.log(var0), math.log(0.5)])
    res = minimize(
        _emos_negloglik_with_spread,
        x0,
        args=(raw, spread, y),
        method="Nelder-Mead",
        options={"maxiter": 1000, "xatol": 1e-3, "fatol": 1e-4},
    )
    a, b, log_c, log_d = res.x
    c = math.exp(log_c)
    d = math.exp(log_d)
    mu_train = a + b * raw
    sigma_train = np.sqrt(np.maximum(c + d * spread**2, MIN_SIGMA_F**2))
    return EmosFit(
        a=float(a),
        b=float(b),
        c=float(c),
        d=float(d),
        sigma_const=None,
        n_train=int(raw.size),
        train_crps=float(_crps_gaussian(mu_train, sigma_train, y).mean()),
        train_rmse=float(np.sqrt(((mu_train - y) ** 2).mean())),
    )


def apply_emos(
    fit: EmosFit, raw: float, spread: float | None = None
) -> tuple[float, float]:
    """Apply persisted coefficients to a single raw forecast. Returns (mu, sigma)."""
    mu = fit.a + fit.b * raw
    if fit.sigma_const is not None:
        return float(mu), max(MIN_SIGMA_F, fit.sigma_const)
    c = fit.c if fit.c is not None else MIN_SIGMA_F**2
    d = fit.d if fit.d is not None else 0.0
    sp = float(spread) if spread is not None else 0.0
    var = c + d * sp**2
    return float(mu), max(MIN_SIGMA_F, math.sqrt(max(var, MIN_SIGMA_F**2)))


# ---------------------------------------------------------------------------
# Training data assembly
# ---------------------------------------------------------------------------


def _fetch_training_pairs(
    cur,
    station_id: int,
    source: str,
    lead_day: int,
    *,
    end_date: dt.date,
    lookback_days: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    """Return (raw_predictions, observations, spread_or_None) aligned by date.

    Training pair = forecast issued ``lead_day`` days before target_date,
    matched against the corresponding ``observations`` row (NOAA or WU).
    """
    cutoff = end_date - dt.timedelta(days=lookback_days)
    # Pick the latest forecast row at the desired lead — i.e. last run_time
    # whose run-time DATE = target_date - lead_day.
    cur.execute(
        """
        WITH ranked AS (
            SELECT
                f.target_date,
                f.predicted_max_f,
                f.predicted_std_f,
                f.run_time,
                ROW_NUMBER() OVER (
                    PARTITION BY f.target_date
                    ORDER BY f.run_time DESC
                ) AS rn
            FROM forecasts f
            WHERE f.station_id = %s
              AND f.source     = %s
              AND f.target_date BETWEEN %s AND %s
              AND f.predicted_max_f IS NOT NULL
              -- run_time on the target_date - lead_day local-UTC day
              AND (f.target_date - DATE(f.run_time AT TIME ZONE 'UTC')) = %s
        )
        SELECT r.target_date, r.predicted_max_f, r.predicted_std_f
        FROM ranked r
        JOIN observations o
          ON o.station_id = %s
         AND o.obs_date   = r.target_date
         AND o.observed_max_f IS NOT NULL
        WHERE r.rn = 1
        ORDER BY r.target_date
        """,
        (station_id, source, cutoff, end_date, lead_day, station_id),
    )
    rows = cur.fetchall()
    if not rows:
        return np.array([]), np.array([]), None

    target_dates = [r[0] for r in rows]
    raw = np.array([float(r[1]) for r in rows], dtype=float)
    spread = np.array(
        [float(r[2]) if r[2] is not None else float("nan") for r in rows],
        dtype=float,
    )

    # Match observations once (we already filtered by IS NOT NULL); fetch the
    # finalised observation per target. Prefer wunderground:historical (since
    # that's the resolution source), else noaa:ghcnd.
    cur.execute(
        """
        SELECT obs_date, source, observed_max_f
        FROM observations
        WHERE station_id = %s
          AND obs_date = ANY(%s)
          AND observed_max_f IS NOT NULL
          AND source IN ('wunderground:historical', 'noaa:ghcnd')
        """,
        (station_id, target_dates),
    )
    by_date: dict[dt.date, float] = {}
    for d, src, val in cur.fetchall():
        if d in by_date and src != "wunderground:historical":
            continue
        by_date[d] = float(val)
    y = np.array(
        [by_date.get(d, float("nan")) for d in target_dates], dtype=float
    )

    finite_mask = np.isfinite(raw) & np.isfinite(y)
    raw = raw[finite_mask]
    y = y[finite_mask]
    spread = spread[finite_mask]
    if not np.any(np.isfinite(spread)):
        return raw, y, None
    return raw, y, spread


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


UPSERT_COEFS_SQL = """
INSERT INTO postprocess_coefs
    (model_id, station_id, source, lead_day, fit_at, n_train,
     a, b, c, d, sigma_floor, train_metrics)
VALUES (%s, %s, %s, %s, now(), %s, %s, %s, %s, %s, %s, %s::jsonb)
"""


def _persist_fit(
    cur,
    *,
    station_id: int,
    source: str,
    lead_day: int,
    fit: EmosFit,
) -> None:
    metrics = {
        "train_crps": fit.train_crps,
        "train_rmse": fit.train_rmse,
        "n_train": fit.n_train,
    }
    cur.execute(
        UPSERT_COEFS_SQL,
        (
            EMOS_MODEL_ID,
            station_id,
            source,
            lead_day,
            fit.n_train,
            fit.a,
            fit.b,
            fit.c,
            fit.d,
            MIN_SIGMA_F,
            json.dumps(metrics),
        ),
    )


def latest_fit_for(
    cur,
    *,
    station_id: int,
    source: str,
    lead_day: int,
) -> EmosFit | None:
    cur.execute(
        """
        SELECT a, b, c, d, n_train
        FROM postprocess_coefs
        WHERE model_id   = %s
          AND station_id = %s
          AND source     = %s
          AND lead_day   = %s
        ORDER BY fit_at DESC
        LIMIT 1
        """,
        (EMOS_MODEL_ID, station_id, source, lead_day),
    )
    row = cur.fetchone()
    if not row:
        return None
    a, b, c, d, n_train = row
    sigma_const = None
    c_v = None if c is None else float(c)
    d_v = None if d is None else float(d)
    if c_v is not None and d_v is None:
        # Treat c as variance constant if d wasn't fitted (legacy fallback).
        sigma_const = math.sqrt(max(c_v, MIN_SIGMA_F**2))
        c_v = None
    return EmosFit(
        a=float(a) if a is not None else 0.0,
        b=float(b) if b is not None else 1.0,
        c=c_v,
        d=d_v,
        sigma_const=sigma_const,
        n_train=int(n_train),
        train_crps=float("nan"),
        train_rmse=float("nan"),
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


DEFAULT_SOURCES = (
    "openmeteo:gfs_seamless",
    "openmeteo:ecmwf_ifs04",
    "openmeteo:icon_seamless",
    "openmeteo:gem_seamless",
    "openmeteo:bestmatch",
    "nws:gridpoint",
    "nws:daily",
    "nbm:station",
    "openmeteo_ens:gfs025",
    "openmeteo_ens:ecmwf_ifs04",
    "openmeteo_ens:icon_seamless",
    "openmeteo_ens:gem_seamless",
)
DEFAULT_LEAD_DAYS = (0, 1, 2, 3, 4, 5, 6, 7)


def fit_postprocess(
    station_slugs: Sequence[str],
    *,
    sources: Sequence[str] = DEFAULT_SOURCES,
    lead_days: Sequence[int] = DEFAULT_LEAD_DAYS,
    end_date: dt.date | None = None,
    lookback_days: int = 365,
) -> dict[str, int]:
    """Fit and persist EMOS coefficients for every (station, source, lead)
    combo with enough history.

    Returns ``{ "fits": <n>, "skipped_too_few": <n>, "skipped_no_data": <n> }``."""
    end_date = end_date or dt.date.today()
    sid_map = station_id_by_slug()

    fits = 0
    skipped_too_few = 0
    skipped_no_data = 0

    with with_conn() as conn, conn.cursor() as cur:
        for slug in station_slugs:
            sid = sid_map.get(slug)
            if sid is None:
                continue
            for src in sources:
                for lead in lead_days:
                    raw, y, spread = _fetch_training_pairs(
                        cur, sid, src, lead,
                        end_date=end_date, lookback_days=lookback_days,
                    )
                    if raw.size == 0:
                        skipped_no_data += 1
                        continue
                    fit = fit_emos(raw, y, spread=spread)
                    if fit is None:
                        skipped_too_few += 1
                        continue
                    _persist_fit(
                        cur, station_id=sid, source=src, lead_day=lead, fit=fit
                    )
                    fits += 1
                    log.debug(
                        "Fit EMOS %s/%s/lead=%d n=%d a=%.2f b=%.3f crps=%.3f",
                        slug, src, lead, fit.n_train, fit.a, fit.b, fit.train_crps,
                    )

    log.info(
        "Postprocess fits=%d skipped_too_few=%d skipped_no_data=%d",
        fits, skipped_too_few, skipped_no_data,
    )
    return {
        "fits": fits,
        "skipped_too_few": skipped_too_few,
        "skipped_no_data": skipped_no_data,
    }
