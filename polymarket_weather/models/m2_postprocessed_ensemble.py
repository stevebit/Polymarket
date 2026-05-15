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
from ..score import (
    Distribution,
    GaussianDist,
    GaussianMixture,
    PercentileCDF,
    predict_bucket_probs,
)
from .baseline import (
    Prediction,
    _active_events_for,
    _latest_forecasts_for,
    _persist_event_probs,
    _persist_prediction,
)
from .postprocess import MIN_SIGMA_F, apply_emos, latest_fit_for
from ..strategy.source_whitelist import source_allowed

# NBM percentile sources written by ``polymarket_weather/data/forecasts.py``
# (``_fetch_nbm_percentiles``). They are pooled into a single PercentileCDF
# rather than 5 separate Gaussian components.
NBM_PERCENTILE_SOURCES = {
    "nbm:p10": 0.10,
    "nbm:p25": 0.25,
    "nbm:p50": 0.50,
    "nbm:p75": 0.75,
    "nbm:p90": 0.90,
}

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


def _percentile_cdf_from_rows(
    nbm_rows: list[tuple[str, float, dt.datetime]],
) -> PercentileCDF | None:
    """Pool ``nbm:p10``..``nbm:p90`` rows into a single :class:`PercentileCDF`.

    Returns ``None`` if fewer than 3 distinct percentile knots are available
    (a CDF needs at least 2 strictly-ordered knots; 3 gives meaningful tail
    information). Knots must be strictly increasing in value — if NBM
    publishes a degenerate set we drop the inner duplicate and rely on the
    Gaussian fall-back path.
    """
    seen: dict[float, float] = {}
    for source, predicted, _run_time in nbm_rows:
        q = NBM_PERCENTILE_SOURCES.get(source)
        if q is None:
            continue
        if predicted is None:
            continue
        seen[q] = float(predicted)
    if len(seen) < 3:
        return None
    quantiles = tuple(sorted(seen.keys()))
    values = tuple(seen[q] for q in quantiles)
    # Strict monotone check; drop on the first violation.
    for i in range(len(values) - 1):
        if values[i + 1] <= values[i]:
            return None
    try:
        return PercentileCDF(quantiles=quantiles, values=values)
    except ValueError:
        return None


def _build_components(
    cur,
    station_id: int,
    target_date: dt.date,
    *,
    as_of: dt.datetime | None = None,
    station_slug: str | None = None,
) -> tuple[list[Component], PercentileCDF | None]:
    """Look up each source's latest forecast and apply EMOS where possible.

    Returns a tuple ``(deterministic_components, nbm_percentile_cdf)``. The
    NBM probabilistic percentiles are pooled into a single
    :class:`PercentileCDF` instead of being treated as 5 separate Gaussian
    components, because their joint distribution is more informative than
    the marginal moments alone (review §4.2).

    If ``as_of`` is set the forecast lookup is bounded to ``run_time <=
    as_of`` and the EMOS fit lookup to ``fit_at <= as_of`` (Phase 1d
    no-lookahead replay).
    """
    forecasts = _latest_forecasts_for(
        cur, station_id, target_date, as_of=as_of
    )
    if station_slug is not None:
        forecasts = [
            row for row in forecasts if source_allowed(station_slug, row[0])
        ]
    nbm_rows = [row for row in forecasts if row[0] in NBM_PERCENTILE_SOURCES]
    deterministic_rows = [
        row for row in forecasts if row[0] not in NBM_PERCENTILE_SOURCES
    ]

    components: list[Component] = []
    for source, predicted, run_time in deterministic_rows:
        raw = float(predicted)
        spread = _ensemble_spread(cur, station_id, source, target_date, run_time)
        lead_day = (target_date - run_time.date()).days
        # ``lead_day`` can be 0 for same-day; clamp negatives to 0.
        lead_day = max(0, lead_day)

        fit = latest_fit_for(
            cur, station_id=station_id, source=source, lead_day=lead_day,
            as_of=as_of,
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

    nbm_cdf = _percentile_cdf_from_rows(nbm_rows)
    if nbm_cdf is not None:
        # Surface the NBM percentile CDF as a single Gaussian-approximated
        # component for the M2 mixture (so weights/mean/std math is
        # consistent). The richer empirical shape is then re-injected at
        # bucket-integration time by averaging with the mixture's CDF.
        components.append(
            Component(
                source="nbm:percentiles",
                mu=float(nbm_cdf.mean),
                sigma=float(max(nbm_cdf.std, MIN_SIGMA_F)),
                # No EMOS fit; NBM percentiles are already calibrated.
                train_crps=None,
                raw_forecast=float(nbm_cdf.mean),
                spread=float(nbm_cdf.std),
            )
        )

    # Lead-0 RAP nowcast component (review §4.5). Only relevant for
    # same-day events; cheap to call but adds a same-day signal that the
    # deterministic 24h forecasts do not have.
    if target_date == _today_for_station(cur, station_id):
        nc = _try_fetch_nowcast(station_id, target_date)
        if nc is not None and nc.expected_max_f is not None:
            components.append(
                Component(
                    source="rap:nowcast",
                    mu=float(nc.expected_max_f),
                    sigma=float(max(nc.rap_rmse_f, MIN_SIGMA_F)),
                    train_crps=None,
                    raw_forecast=float(nc.expected_max_f),
                    spread=float(nc.rap_rmse_f),
                )
            )
    return components, nbm_cdf


def _today_for_station(cur, station_id: int) -> dt.date | None:
    """Return today (UTC) — the RAP nowcast only applies to same-day events."""
    return dt.datetime.now(dt.timezone.utc).date()


def _get_isotonic_fit_cached(model_id: str):
    """Lightweight per-process cache for ``latest_isotonic_fit``.

    The fit is small (a pair of arrays), changes only when
    ``fit_isotonic`` is re-run, and is consulted on **every** event during
    a tick. Caching avoids an extra DB round-trip per event without
    introducing staleness within a single tick.
    """
    # Late import keeps the M2 module light during unit-test imports.
    from .isotonic import latest_isotonic_fit

    cache = _get_isotonic_fit_cached
    fit = getattr(cache, "_cache", {}).get(model_id, "SENTINEL")
    if fit == "SENTINEL":
        try:
            fit = latest_isotonic_fit(model_id)
        except Exception as exc:  # noqa: BLE001
            log.info("Isotonic fit lookup failed for %s: %s", model_id, exc)
            fit = None
        if not hasattr(cache, "_cache"):
            cache._cache = {}
        cache._cache[model_id] = fit
    return fit


def _try_fetch_nowcast(station_id: int, target_date: dt.date):
    """Resolve the station slug and call ``fetch_nowcast``. Best-effort: on
    any failure we silently fall back (component count drops by one)."""
    try:
        # Late import: keeps the M2 module import-graph tight when running
        # without the data layer (e.g. unit tests).
        from ..data.nowcast import fetch_nowcast
        from ..db import station_id_by_slug
        import asyncio

        sid_to_slug = {sid: slug for slug, sid in station_id_by_slug().items()}
        slug = sid_to_slug.get(station_id)
        if slug is None:
            return None
        return asyncio.run(fetch_nowcast(slug, target_date))
    except Exception:  # noqa: BLE001
        return None


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
    as_of: dt.datetime | None = None,
    station_slug: str | None = None,
) -> tuple[Prediction, Distribution] | None:
    components, nbm_cdf = _build_components(
        cur, station_id, target_date, as_of=as_of, station_slug=station_slug
    )
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

    # Critical fix (review §2.2): the climatology floor only meant something
    # if it actually reached the bucket integration step. Previously
    # ``sigma_total`` was stored on the prediction row but the un-floored
    # mixture was fed to ``predict_bucket_probs``, so the floor was a no-op
    # for trading. Now: if the floor is tighter than the mixture's natural
    # spread, integrate against a single Gaussian centred at the mixture
    # mean with sigma = sigma_total. Otherwise keep the richer mixture
    # shape (the mixture is already wider than the floor).
    floor_applied = sigma_total > float(mixture.std) + 1e-9
    if floor_applied:
        distribution: Distribution = GaussianDist(
            mean_f=float(mixture.mean), std_f=float(sigma_total)
        )
    else:
        distribution = mixture

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
            "floor_applied": bool(floor_applied),
            "nbm_percentile_cdf": (
                None
                if nbm_cdf is None
                else {
                    "quantiles": list(nbm_cdf.quantiles),
                    "values": list(nbm_cdf.values),
                }
            ),
        },
    )
    return pred, distribution


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_m2_predictions(
    station_slugs: Iterable[str],
    target_dates: Iterable[dt.date],
    *,
    as_of: dt.datetime | None = None,
) -> dict[str, int]:
    """Run M2 for each (station, target_date).

    If ``as_of`` is set, predictions are produced **as if** we were standing
    at ``as_of`` (no leakage). Persisted ``run_time = as_of`` so calibration
    and backtest see a consistent temporal anchor.
    """
    sid_map = station_id_by_slug()
    n_pred = 0
    n_probs = 0
    run_time = as_of if as_of is not None else dt.datetime.now(dt.timezone.utc)
    target_dates = list(target_dates)
    with with_conn() as conn, conn.cursor() as cur:
        for slug in station_slugs:
            sid = sid_map.get(slug)
            if sid is None:
                continue
            for target in target_dates:
                events = _active_events_for(cur, sid, target)
                result = predict_m2_for(
                    cur, sid, target, run_time=run_time, as_of=as_of,
                    station_slug=slug,
                )
                if result is None:
                    continue
                pred, distribution = result
                _persist_prediction(cur, pred)
                n_pred += 1
                if not events:
                    continue
                iso_fit = _get_isotonic_fit_cached(MODEL_M2)
                for event_slug, buckets in events:
                    probs = predict_bucket_probs(buckets, distribution)
                    # Phase 6: optional per-model isotonic recalibration is
                    # the *last* step before persistence. ``apply_isotonic``
                    # renormalises so probs still sum to 1.
                    if iso_fit is not None:
                        from .isotonic import apply_isotonic
                        probs = apply_isotonic(iso_fit, probs)
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
