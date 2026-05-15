"""Fit and load a logistic maker-fill-probability curve.

Plan Phase 7 (review §5.4): once the Phase 4 backfill produces a maker
fill ledger (orders posted vs filled), we fit

    logit P(fill) = a + b * distance_from_mid + c * lead_days

per station. Until that ledger exists the function falls back to the
conservative 0.10 default. The curve is stored as a JSONB blob in
``maker_fill_coefs`` so future feature additions don't need a migration.

The schema migration is ``007_maker_fill_coefs.sql``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from ..db import with_conn

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MakerFillFit:
    intercept: float
    coef_distance: float
    coef_lead: float
    n_train: int
    train_auc: float | None


def _logit_predict(x_distance: float, x_lead: float, fit: MakerFillFit) -> float:
    z = (
        fit.intercept
        + fit.coef_distance * x_distance
        + fit.coef_lead * x_lead
    )
    # Numerically-stable sigmoid.
    if z >= 0:
        ez = np.exp(-z)
        return float(1.0 / (1.0 + ez))
    ez = np.exp(z)
    return float(ez / (1.0 + ez))


def fit_maker_fill(
    distances: Sequence[float],
    leads: Sequence[float],
    filled: Sequence[int],
) -> MakerFillFit | None:
    """Fit ``LogisticRegression(filled ~ distance + lead)`` on the inputs.

    Requires at least 50 events and both classes present. Returns ``None``
    otherwise (caller keeps the conservative default).
    """
    if len(distances) < 50 or len({int(f) for f in filled}) < 2:
        return None
    from sklearn.linear_model import LogisticRegression  # local import

    X = np.column_stack(
        [
            np.asarray(distances, dtype=float),
            np.asarray(leads, dtype=float),
        ]
    )
    y = np.asarray(filled, dtype=int)
    model = LogisticRegression(max_iter=200, solver="liblinear")
    model.fit(X, y)
    auc: float | None = None
    try:
        from sklearn.metrics import roc_auc_score
        scores = model.predict_proba(X)[:, 1]
        auc = float(roc_auc_score(y, scores))
    except Exception:  # noqa: BLE001
        auc = None
    fit = MakerFillFit(
        intercept=float(model.intercept_[0]),
        coef_distance=float(model.coef_[0][0]),
        coef_lead=float(model.coef_[0][1]),
        n_train=int(X.shape[0]),
        train_auc=auc,
    )
    persist_maker_fill_fit(fit)
    return fit


PERSIST_SQL = """
INSERT INTO maker_fill_coefs (n_train, coef, train_auc)
VALUES (%s, %s::jsonb, %s)
"""


def persist_maker_fill_fit(fit: MakerFillFit) -> None:
    payload = {
        "intercept": fit.intercept,
        "coef_distance": fit.coef_distance,
        "coef_lead": fit.coef_lead,
        "features": ["distance_from_mid", "lead_days"],
    }
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            PERSIST_SQL,
            (fit.n_train, json.dumps(payload), fit.train_auc),
        )


def latest_maker_fill_fit() -> MakerFillFit | None:
    try:
        with with_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT coef, n_train, train_auc
                FROM maker_fill_coefs
                ORDER BY fit_at DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        log.debug("maker_fill_coefs lookup failed: %s", exc)
        return None
    if row is None:
        return None
    coef, n_train, train_auc = row
    if isinstance(coef, str):
        coef = json.loads(coef)
    return MakerFillFit(
        intercept=float(coef.get("intercept", 0.0)),
        coef_distance=float(coef.get("coef_distance", 0.0)),
        coef_lead=float(coef.get("coef_lead", 0.0)),
        n_train=int(n_train),
        train_auc=None if train_auc is None else float(train_auc),
    )


def fill_prob(
    distance_from_mid: float | None,
    lead_days: float | None,
) -> float | None:
    """Return the learned maker fill probability, or ``None`` if no fit
    has been persisted yet (caller keeps its conservative default)."""
    fit = latest_maker_fill_fit()
    if fit is None:
        return None
    return _logit_predict(
        float(distance_from_mid) if distance_from_mid is not None else 0.0,
        float(lead_days) if lead_days is not None else 0.0,
        fit,
    )
