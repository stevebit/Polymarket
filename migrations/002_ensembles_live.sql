-- 002_ensembles_live.sql
-- Phase 1b/1c additions:
--  * ``forecast_members``: per-member ensemble forecast values (one row per
--    member per target_date per source per run). Keyed alongside the
--    deterministic ``forecasts`` table; the latter still holds the ensemble
--    mean (or per-source deterministic value) for fast access.
--  * ``postprocess_coefs``: persisted EMOS / DRN coefficients fit per
--    (station, source, lead_day). Used by Phase 2 ``models.postprocess``.

CREATE TABLE IF NOT EXISTS forecast_members (
    station_id      INT NOT NULL REFERENCES stations(station_id),
    target_date     DATE NOT NULL,
    source          TEXT NOT NULL,
    run_time        TIMESTAMPTZ NOT NULL,
    member_id       SMALLINT NOT NULL,
    predicted_max_f NUMERIC,
    raw             JSONB,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (station_id, target_date, source, run_time, member_id)
);

CREATE INDEX IF NOT EXISTS forecast_members_station_date_idx
    ON forecast_members (station_id, target_date);

CREATE TABLE IF NOT EXISTS postprocess_coefs (
    model_id            TEXT NOT NULL,
    station_id          INT NOT NULL REFERENCES stations(station_id),
    source              TEXT NOT NULL,
    lead_day            SMALLINT NOT NULL,
    fit_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    n_train             INT NOT NULL,
    -- EMOS-style: mu = a + b * raw_forecast; sigma = exp(c + d * spread)
    a                   NUMERIC,
    b                   NUMERIC,
    c                   NUMERIC,
    d                   NUMERIC,
    sigma_floor         NUMERIC,           -- F, never let predictive sigma fall below
    train_metrics       JSONB,             -- {"crps": ..., "rmse": ..., ...}
    PRIMARY KEY (model_id, station_id, source, lead_day, fit_at)
);

CREATE INDEX IF NOT EXISTS postprocess_coefs_lookup_idx
    ON postprocess_coefs (model_id, station_id, source, lead_day, fit_at DESC);
