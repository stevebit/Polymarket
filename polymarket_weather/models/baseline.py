"""Baseline distributional models for daily TMAX-F.

Two models are exposed; both write to ``predictions`` and ``bucket_probs``
keyed by ``model_id``.

* **M0 best-source**: per ``(station, run_date)``, pick the forecast source
  with the lowest 30-day rolling MAE versus observations. Predictive
  distribution = ``N(mu = predicted_max_f, sigma = source's recent MAE)``.
* **M1 ensemble Gaussian**: weighted mean across all forecast sources, with
  weights inverse to per-source 30-day MAE. ``sigma^2`` = within-ensemble
  variance + station-level residual variance over a 90-day window.

Persistence is incremental and idempotent. The bucket-prob writer renormalises
across each event so probabilities sum to 1.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from dataclasses import dataclass
from typing import Iterable

from ..db import station_id_by_slug, with_conn
from ..score import BucketBounds, event_probabilities
from ..strategy.source_whitelist import source_allowed

log = logging.getLogger(__name__)

MODEL_M0 = "m0_best_source"
MODEL_M1 = "m1_ensemble_gaussian"

DEFAULT_FALLBACK_SIGMA = 4.0  # F, used when MAE history is empty / too small


# ---------------------------------------------------------------------------
# Helpers: latest forecasts and recent MAE per source.
# ---------------------------------------------------------------------------


def _latest_forecasts_for(
    cur,
    station_id: int,
    target_date: dt.date,
    *,
    as_of: dt.datetime | None = None,
) -> list[tuple[str, float, dt.datetime]]:
    """Return ``(source, predicted_max_f, run_time)`` for the most recent
    run_time per source for the given (station, target_date).

    If ``as_of`` is set, only forecast cycles with ``run_time <= as_of`` are
    considered. Used by the ``predict --as-of`` historical-replay mode to
    guarantee no leakage from future cycles.
    """
    if as_of is None:
        cur.execute(
            """
            SELECT DISTINCT ON (source) source, predicted_max_f, run_time
            FROM forecasts
            WHERE station_id = %s
              AND target_date = %s
              AND predicted_max_f IS NOT NULL
            ORDER BY source, run_time DESC
            """,
            (station_id, target_date),
        )
    else:
        cur.execute(
            """
            SELECT DISTINCT ON (source) source, predicted_max_f, run_time
            FROM forecasts
            WHERE station_id = %s
              AND target_date = %s
              AND predicted_max_f IS NOT NULL
              AND run_time   <= %s
            ORDER BY source, run_time DESC
            """,
            (station_id, target_date, as_of),
        )
    return list(cur.fetchall())


def _source_mae(
    cur,
    station_id: int,
    source: str,
    *,
    lookback_days: int,
    end_date: dt.date,
    as_of: dt.datetime | None = None,
) -> tuple[float | None, int]:
    """Most recent run per (target_date) for that source vs observed.

    If ``as_of`` is set, only forecast cycles with ``run_time <= as_of`` are
    considered (no leakage from later cycles). ``end_date`` already bounds
    ``target_date``; the as_of clause is the temporal cutoff on the forecast
    *issue time*.
    """
    if as_of is None:
        cur.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (target_date)
                    target_date,
                    predicted_max_f
                FROM forecasts
                WHERE station_id = %s
                  AND source     = %s
                  AND target_date BETWEEN %s AND %s
                  AND predicted_max_f IS NOT NULL
                ORDER BY target_date, run_time DESC
            )
            SELECT AVG(ABS(l.predicted_max_f - o.observed_max_f))::float8 AS mae,
                   COUNT(*)                                                AS n
            FROM latest l
            JOIN observations o
              ON o.station_id = %s
             AND o.obs_date   = l.target_date
             AND o.observed_max_f IS NOT NULL
            """,
            (
                station_id, source,
                end_date - dt.timedelta(days=lookback_days), end_date,
                station_id,
            ),
        )
    else:
        cur.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (target_date)
                    target_date,
                    predicted_max_f
                FROM forecasts
                WHERE station_id = %s
                  AND source     = %s
                  AND target_date BETWEEN %s AND %s
                  AND predicted_max_f IS NOT NULL
                  AND run_time    <= %s
                ORDER BY target_date, run_time DESC
            )
            SELECT AVG(ABS(l.predicted_max_f - o.observed_max_f))::float8 AS mae,
                   COUNT(*)                                                AS n
            FROM latest l
            JOIN observations o
              ON o.station_id = %s
             AND o.obs_date   = l.target_date
             AND o.observed_max_f IS NOT NULL
             AND o.ingested_at <= %s
            """,
            (
                station_id, source,
                end_date - dt.timedelta(days=lookback_days), end_date,
                as_of,
                station_id,
                as_of,
            ),
        )
    row = cur.fetchone()
    if not row or row[0] is None:
        return None, 0
    return float(row[0]), int(row[1])


def _station_residual_std(
    cur,
    station_id: int,
    *,
    end_date: dt.date,
    lookback_days: int = 90,
    as_of: dt.datetime | None = None,
) -> float | None:
    """Station-level residual sigma using the per-target_date forecast mean
    across all sources vs observed.

    If ``as_of`` is given, forecast cycles are filtered to ``run_time <=
    as_of`` and observations to ``ingested_at <= as_of`` (no leakage)."""
    if as_of is None:
        cur.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (source, target_date)
                    source, target_date, predicted_max_f
                FROM forecasts
                WHERE station_id = %s
                  AND target_date BETWEEN %s AND %s
                  AND predicted_max_f IS NOT NULL
                ORDER BY source, target_date, run_time DESC
            ),
            per_day AS (
                SELECT target_date, AVG(predicted_max_f)::float8 AS mean_f
                FROM latest
                GROUP BY target_date
            )
            SELECT STDDEV_SAMP(p.mean_f - o.observed_max_f)::float8
            FROM per_day p
            JOIN observations o
              ON o.station_id = %s
             AND o.obs_date   = p.target_date
             AND o.observed_max_f IS NOT NULL
            """,
            (
                station_id,
                end_date - dt.timedelta(days=lookback_days),
                end_date,
                station_id,
            ),
        )
    else:
        cur.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (source, target_date)
                    source, target_date, predicted_max_f
                FROM forecasts
                WHERE station_id = %s
                  AND target_date BETWEEN %s AND %s
                  AND predicted_max_f IS NOT NULL
                  AND run_time    <= %s
                ORDER BY source, target_date, run_time DESC
            ),
            per_day AS (
                SELECT target_date, AVG(predicted_max_f)::float8 AS mean_f
                FROM latest
                GROUP BY target_date
            )
            SELECT STDDEV_SAMP(p.mean_f - o.observed_max_f)::float8
            FROM per_day p
            JOIN observations o
              ON o.station_id = %s
             AND o.obs_date   = p.target_date
             AND o.observed_max_f IS NOT NULL
             AND o.ingested_at <= %s
            """,
            (
                station_id,
                end_date - dt.timedelta(days=lookback_days),
                end_date,
                as_of,
                station_id,
                as_of,
            ),
        )
    row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def _active_events_for(
    cur, station_id: int, target_date: dt.date
) -> list[tuple[str, list[BucketBounds]]]:
    cur.execute(
        """
        SELECT e.event_slug, b.bucket_label, b.lo_f, b.hi_f
        FROM pm_events e
        JOIN pm_buckets b ON b.event_slug = e.event_slug
        WHERE e.station_id = %s
          AND e.target_date = %s
        ORDER BY e.event_slug, b.bucket_label
        """,
        (station_id, target_date),
    )
    grouped: dict[str, list[BucketBounds]] = {}
    for slug, label, lo, hi in cur.fetchall():
        grouped.setdefault(slug, []).append(
            BucketBounds(
                label=label,
                lo_f=None if lo is None else float(lo),
                hi_f=None if hi is None else float(hi),
            )
        )
    return list(grouped.items())


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


UPSERT_PRED_SQL = """
INSERT INTO predictions
    (model_id, station_id, target_date, run_time, mean_f, std_f,
     features_jsonb, ingested_at)
VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, now())
ON CONFLICT (model_id, station_id, target_date, run_time) DO UPDATE SET
    mean_f         = EXCLUDED.mean_f,
    std_f          = EXCLUDED.std_f,
    features_jsonb = EXCLUDED.features_jsonb,
    ingested_at    = now()
"""

UPSERT_BUCKET_PROB_SQL = """
INSERT INTO bucket_probs (model_id, event_slug, bucket_label, run_time, prob)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (model_id, event_slug, bucket_label, run_time) DO UPDATE SET
    prob = EXCLUDED.prob
"""


@dataclass
class Prediction:
    model_id: str
    station_id: int
    target_date: dt.date
    run_time: dt.datetime
    mean_f: float
    std_f: float
    features: dict


def _persist_prediction(cur, p: Prediction) -> None:
    cur.execute(
        UPSERT_PRED_SQL,
        (
            p.model_id,
            p.station_id,
            p.target_date,
            p.run_time,
            p.mean_f,
            p.std_f,
            json.dumps(p.features),
        ),
    )


def _persist_event_probs(
    cur,
    *,
    model_id: str,
    event_slug: str,
    run_time: dt.datetime,
    probs: dict[str, float],
) -> None:
    for label, prob in probs.items():
        cur.execute(
            UPSERT_BUCKET_PROB_SQL,
            (model_id, event_slug, label, run_time, prob),
        )


# ---------------------------------------------------------------------------
# M0
# ---------------------------------------------------------------------------


def predict_m0_for(
    cur,
    station_id: int,
    target_date: dt.date,
    *,
    run_time: dt.datetime,
    mae_lookback_days: int = 30,
    as_of: dt.datetime | None = None,
    station_slug: str | None = None,
) -> Prediction | None:
    forecasts = _latest_forecasts_for(cur, station_id, target_date, as_of=as_of)
    if station_slug is not None:
        forecasts = [
            row for row in forecasts if source_allowed(station_slug, row[0])
        ]
    if not forecasts:
        return None

    best: tuple[float, str, float] | None = None  # (mae, source, predicted)
    mae_ref_date = target_date - dt.timedelta(days=1)
    source_maes: dict[str, dict] = {}
    for source, predicted, _run in forecasts:
        mae, n = _source_mae(
            cur, station_id, source,
            lookback_days=mae_lookback_days, end_date=mae_ref_date,
            as_of=as_of,
        )
        source_maes[source] = {"mae": mae, "n": n, "predicted": float(predicted)}
        if mae is None or n < 3:
            continue
        if best is None or mae < best[0]:
            best = (mae, source, float(predicted))

    if best is None:
        # No source had enough history — fall back to first source with a
        # default sigma so we still produce *something* downstream.
        source, predicted, _ = forecasts[0]
        chosen = {
            "source": source,
            "predicted": float(predicted),
            "sigma": DEFAULT_FALLBACK_SIGMA,
            "fallback": True,
        }
        return Prediction(
            model_id=MODEL_M0,
            station_id=station_id,
            target_date=target_date,
            run_time=run_time,
            mean_f=float(predicted),
            std_f=DEFAULT_FALLBACK_SIGMA,
            features={"chosen": chosen, "source_maes": source_maes},
        )

    mae, source, predicted = best
    sigma = max(mae, 0.5)
    return Prediction(
        model_id=MODEL_M0,
        station_id=station_id,
        target_date=target_date,
        run_time=run_time,
        mean_f=predicted,
        std_f=sigma,
        features={
            "chosen": {"source": source, "mae": mae, "sigma": sigma},
            "source_maes": source_maes,
        },
    )


# ---------------------------------------------------------------------------
# M1
# ---------------------------------------------------------------------------


def predict_m1_for(
    cur,
    station_id: int,
    target_date: dt.date,
    *,
    run_time: dt.datetime,
    mae_lookback_days: int = 30,
    residual_lookback_days: int = 90,
    as_of: dt.datetime | None = None,
    station_slug: str | None = None,
) -> Prediction | None:
    forecasts = _latest_forecasts_for(cur, station_id, target_date, as_of=as_of)
    if station_slug is not None:
        forecasts = [
            row for row in forecasts if source_allowed(station_slug, row[0])
        ]
    if not forecasts:
        return None

    mae_ref_date = target_date - dt.timedelta(days=1)
    weighted: list[tuple[str, float, float]] = []  # (source, predicted, weight)
    source_maes: dict[str, dict] = {}
    for source, predicted, _run in forecasts:
        mae, n = _source_mae(
            cur, station_id, source,
            lookback_days=mae_lookback_days, end_date=mae_ref_date,
            as_of=as_of,
        )
        source_maes[source] = {"mae": mae, "n": n, "predicted": float(predicted)}
        if mae is None or n < 3 or mae <= 0:
            # Equal weight fallback; will be normalised below.
            weight = 1.0 / max(DEFAULT_FALLBACK_SIGMA, 1.0)
        else:
            weight = 1.0 / mae
        weighted.append((source, float(predicted), weight))

    total_w = sum(w for _, _, w in weighted)
    if total_w <= 0:
        return None
    mu = sum(p * w for _, p, w in weighted) / total_w
    within_var = (
        sum(w * (p - mu) ** 2 for _, p, w in weighted) / total_w
        if len(weighted) > 1
        else 0.0
    )
    residual_sigma = _station_residual_std(
        cur, station_id,
        end_date=mae_ref_date,
        lookback_days=residual_lookback_days,
        as_of=as_of,
    )
    residual_var = (
        residual_sigma ** 2
        if residual_sigma is not None
        else DEFAULT_FALLBACK_SIGMA ** 2
    )
    sigma = math.sqrt(within_var + residual_var)
    # Sigma floor (review §2.7): the old 0.75°F floor was tighter than the
    # observed station-level residual MAE for almost every city, which led
    # to over-confident bucket probabilities at the tails. Floor at
    # ``max(2.0, residual_sigma)`` so the predictive never claims to know
    # more than the actual recent residual variance.
    sigma_floor = max(2.0, residual_sigma if residual_sigma is not None else 0.0)
    sigma = max(sigma, sigma_floor)

    return Prediction(
        model_id=MODEL_M1,
        station_id=station_id,
        target_date=target_date,
        run_time=run_time,
        mean_f=float(mu),
        std_f=float(sigma),
        features={
            "weights_sum": total_w,
            "within_var": within_var,
            "residual_sigma": residual_sigma,
            "source_maes": source_maes,
        },
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_predictions(
    station_slugs: Iterable[str],
    target_dates: Iterable[dt.date],
    *,
    models: tuple[str, ...] = (MODEL_M0, MODEL_M1),
    as_of: dt.datetime | None = None,
) -> dict[str, int]:
    """Run M0/M1 for each (station, target_date).

    If ``as_of`` is set:
      - the persisted ``run_time`` becomes ``as_of`` (so calibration /
        backtest SQL see the same temporal anchor that bounded the data);
      - every internal SELECT is filtered to ``run_time <= as_of`` and
        ``ingested_at <= as_of`` to guarantee no leakage from data that
        wasn't available at that wall-clock.
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
                preds: list[Prediction] = []
                for model_id in models:
                    if model_id == MODEL_M0:
                        p = predict_m0_for(
                            cur, sid, target, run_time=run_time, as_of=as_of,
                            station_slug=slug,
                        )
                    elif model_id == MODEL_M1:
                        p = predict_m1_for(
                            cur, sid, target, run_time=run_time, as_of=as_of,
                            station_slug=slug,
                        )
                    else:
                        log.warning("Unknown model_id %r — skipping", model_id)
                        continue
                    if p is None:
                        continue
                    preds.append(p)
                    _persist_prediction(cur, p)
                    n_pred += 1

                if not events or not preds:
                    continue

                for event_slug, buckets in events:
                    for p in preds:
                        probs = event_probabilities(buckets, p.mean_f, p.std_f)
                        _persist_event_probs(
                            cur,
                            model_id=p.model_id,
                            event_slug=event_slug,
                            run_time=p.run_time,
                            probs=probs,
                        )
                        n_probs += len(probs)
    log.info("Wrote %d predictions / %d bucket_probs rows", n_pred, n_probs)
    return {"predictions": n_pred, "bucket_probs": n_probs}
