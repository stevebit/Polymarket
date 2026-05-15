-- 007_maker_fill_coefs.sql
--
-- Phase 7 (plan): persist a learned logistic curve for maker fill
-- probability as a function of (distance_from_mid, lead_days). Replaces
-- the conservative 0.10 default in
-- ``polymarket_weather/strategy/edge.py:fill_prob_estimator``.
--
-- ``coef`` is a JSON blob holding the logistic coefficients so we can
-- extend the feature set later without a schema migration.

CREATE TABLE IF NOT EXISTS maker_fill_coefs (
    fit_id     UUID NOT NULL DEFAULT gen_random_uuid(),
    fit_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    n_train    INT NOT NULL,
    coef       JSONB NOT NULL,
    train_auc  DOUBLE PRECISION,
    PRIMARY KEY (fit_id)
);

CREATE INDEX IF NOT EXISTS maker_fill_coefs_latest_idx
    ON maker_fill_coefs (fit_at DESC);
