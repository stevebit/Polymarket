-- 006_isotonic_calibration.sql
--
-- Phase 6 (plan): persist a per-model isotonic recalibration of bucket
-- probabilities. The fit is computed by ``polymarket_weather.models.isotonic``
-- and is consumed at predict time as a final monotone transform on top of
-- the raw bucket probabilities.
--
-- One row per ``model_id`` (latest ``fit_at`` wins).

CREATE TABLE IF NOT EXISTS isotonic_calibration (
    model_id     TEXT NOT NULL,
    x_knots      DOUBLE PRECISION[] NOT NULL,
    y_knots      DOUBLE PRECISION[] NOT NULL,
    n_train      INT NOT NULL,
    train_brier  DOUBLE PRECISION,
    train_logloss DOUBLE PRECISION,
    fit_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (model_id, fit_at)
);

CREATE INDEX IF NOT EXISTS isotonic_calibration_latest_idx
    ON isotonic_calibration (model_id, fit_at DESC);
