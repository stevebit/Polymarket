"""M2 — postprocessed Gaussian-mixture model.

Per ``(station, target_date)``:

1. For each available forecast source, look up the latest deterministic
   prediction (``mean_f``) and any available ensemble spread
   (``predicted_std_f`` or computed from ``forecast_members``).
2. Compute the lead day = target_date - run_time.date(). Look up the most
   recent persisted EMOS fit for that ``(station, source, lead_day)`` and
   transform the raw forecast into a calibrated ``(mu, sigma)``.
3. Weight each component by ``exp(-train_crps)`` (smaller training CRPS =
   better source). Sources without a fit fall back to identity (raw mean,
   inflated sigma).
4. Form a :class:`GaussianMixture` and integrate against the active
   ``pm_buckets`` for the event.

Climatology floor: ``sigma_total`` is never allowed below the climatological
``tmax_std`` for that day-of-year, so the predictive doesn't get
over-confident when sources happen to all agree by luck.

Persistence: writes ``predictions(model_id='m2_postprocessed_ens')`` and
``bucket_probs`` rows. Mean / std stored are the mixture moments.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from dataclasses import dataclass
from typing import Iterable

from ..db import station_id_by_slug, with_conn
from ..score import GaussianMixture, predict_bucket_probs
from .baseline import (
    Prediction,
    _active_events_for,
    _latest_forecasts_for,
    _persist_event_probs,
    _persist_prediction,
)
from .postprocess import MIN_SIGMA_F, apply_emos, latest_fit_for

log = logging.getLogger(__name__)

MODEL_M2 = "m2_postprocessed_ens"

# Sigma floor relative to climatology. We never let total sigma drop below
# CLIM_FLOOR_FRAC * tmax_std for that doy.
CLIM_FLOOR_FRAC = 0.4
# Default sigma when EMOS not yet fit and we have no spread.
DEFAULT_SIGMA_F = 4.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _climatology_std(
    cur, station_id: int, target_date: dt.date
) -> float | None:
    doy = (target_date.timetuple().tm_yday)
    cur.execute(
        "SELECT tmax_std FROM climatology WHERE station_id = %s AND doy = %s",
        (station_id, doy),
    )
    row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def _ensemble_spread(
    cur,
    station_id: int,
    source: str,
    target_date: dt.date,
    run_time: dt.datetime,
) -> float | None:
    """Sample stddev across ensemble members for the same (source, run_time)."""
    cur.execute(
        """
        SELECT STDDEV_SAMP(predicted_max_f)::float8
        FROM forecast_members
        WHERE station_id  = %s
          AND source      = %s
          AND target_date = %s
          AND run_time    = %s
          AND predicted_max_f IS NOT NULL
        """,
        (station_id, source, target_date, run_time),
    )
    row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


@dataclass
class Component:
    source: str
    mu: float
    sigma: float
    train_crps: float | None
    raw_forecast: float
    spread: float | None


def _build_components(
    cur,
    station_id: int,
    target_date: dt.date,
) -> list[Component]:
    """Look up each source's latest forecast and apply EMOS where possible."""
    forecasts = _latest_forecasts_for(cur, station_id, target_date)
    components: list[Component] = []
    for source, predicted, run_time in forecasts:
        raw = float(predicted)
        spread = _ensemble_spread(cur, station_id, source, target_date, run_time)
        lead_day = (target_date - run_time.date()).days
        # ``lead_day`` can be 0 for same-day; clamp negatives to 0.
        lead_day = max(0, lead_day)

        fit = latest_fit_for(
            cur, station_id=station_id, source=source, lead_day=lead_day
        )
        if fit is None:
            mu = raw
            sigma = (
                spread
                if spread is not None and spread > MIN_SIGMA_F
                else DEFAULT_SIGMA_F
            )
            train_crps = None
        else:
            mu, sigma = apply_emos(fit, raw, spread=spread)
            train_crps = (
                None
                if fit.train_crps is None or math.isnan(fit.train_crps)
                else fit.train_crps
            )

        components.append(
            Component(
                source=source,
                mu=float(mu),
                sigma=float(max(sigma, MIN_SIGMA_F)),
                train_crps=train_crps,
                raw_forecast=raw,
                spread=spread,
            )
        )
    return components


def _component_weights(components: list[Component]) -> list[float]:
    """Weight by ``exp(-train_crps)``. Sources without a fit get a
    pessimistic weight equal to the median of fit weights."""
    fit_crps = [c.train_crps for c in components if c.train_crps is not None]
    if fit_crps:
        median_crps = sorted(fit_crps)[len(fit_crps) // 2]
    else:
        median_crps = 1.0
    weights: list[float] = []
    for c in components:
        crps = c.train_crps if c.train_crps is not None else 2.0 * median_crps
        weights.append(math.exp(-crps))
    s = sum(weights)
    if s <= 0:
        return [1.0 / max(len(weights), 1)] * len(weights)
    return [w / s for w in weights]


def predict_m2_for(
    cur,
    station_id: int,
    target_date: dt.date,
    *,
    run_time: dt.datetime,
) -> tuple[Prediction, GaussianMixture] | None:
    components = _build_components(cur, station_id, target_date)
    if not components:
        return None
    weights = _component_weights(components)

    means = tuple(c.mu for c in components)
    stds = tuple(c.sigma for c in components)
    mixture = GaussianMixture(weights=tuple(weights), means=means, stds=stds)

    sigma_total = max(mixture.std, MIN_SIGMA_F)
    clim_std = _climatology_std(cur, station_id, target_date)
    if clim_std is not None:
        sigma_total = max(sigma_total, CLIM_FLOOR_FRAC * clim_std)

    pred = Prediction(
        model_id=MODEL_M2,
        station_id=station_id,
        target_date=target_date,
        run_time=run_time,
        mean_f=float(mixture.mean),
        std_f=float(sigma_total),
        features={
            "components": [
                {
                    "source": c.source,
                    "mu": c.mu,
                    "sigma": c.sigma,
                    "weight": w,
                    "train_crps": c.train_crps,
                    "raw_forecast": c.raw_forecast,
                    "spread": c.spread,
                }
                for c, w in zip(components, weights)
            ],
            "mixture_mean": mixture.mean,
            "mixture_std": float(mixture.std),
            "sigma_total": float(sigma_total),
            "climatology_std": clim_std,
        },
    )
    return pred, mixture


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_m2_predictions(
    station_slugs: Iterable[str],
    target_dates: Iterable[dt.date],
) -> dict[str, int]:
    sid_map = station_id_by_slug()
    n_pred = 0
    n_probs = 0
    run_time = dt.datetime.now(dt.timezone.utc)
    target_dates = list(target_dates)
    with with_conn() as conn, conn.cursor() as cur:
        for slug in station_slugs:
            sid = sid_map.get(slug)
            if sid is None:
                continue
            for target in target_dates:
                events = _active_events_for(cur, sid, target)
                result = predict_m2_for(cur, sid, target, run_time=run_time)
                if result is None:
                    continue
                pred, mixture = result
                _persist_prediction(cur, pred)
                n_pred += 1
                if not events:
                    continue
                for event_slug, buckets in events:
                    probs = predict_bucket_probs(buckets, mixture)
                    _persist_event_probs(
                        cur,
                        model_id=MODEL_M2,
                        event_slug=event_slug,
                        run_time=run_time,
                        probs=probs,
                    )
                    n_probs += len(probs)
    log.info("M2: predictions=%d bucket_probs=%d", n_pred, n_probs)
    return {"predictions": n_pred, "bucket_probs": n_probs}
