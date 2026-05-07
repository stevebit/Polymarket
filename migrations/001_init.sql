-- 001_init.sql
-- Initial schema for the polymarket_weather research package.
-- All tables live in the default ``public`` schema. Idempotent where possible.
-- Uses Postgres 13+ built-in gen_random_uuid() (no extension required).

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS stations (
    station_id              SERIAL PRIMARY KEY,
    slug                    TEXT NOT NULL UNIQUE,
    polymarket_city_slug    TEXT NOT NULL,
    icao                    TEXT NOT NULL,
    ghcn_id                 TEXT NOT NULL UNIQUE,
    lat                     NUMERIC(8, 5) NOT NULL,
    lon                     NUMERIC(9, 5) NOT NULL,
    tz                      TEXT NOT NULL,
    display_name            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pm_events (
    event_slug      TEXT PRIMARY KEY,
    station_id      INT NOT NULL REFERENCES stations(station_id),
    target_date     DATE NOT NULL,
    gamma_event_id  TEXT,
    raw             JSONB NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pm_events_station_date_idx
    ON pm_events (station_id, target_date);

CREATE TABLE IF NOT EXISTS pm_buckets (
    event_slug      TEXT NOT NULL REFERENCES pm_events(event_slug) ON DELETE CASCADE,
    bucket_label    TEXT NOT NULL,
    lo_f            NUMERIC,            -- NULL means open low end
    hi_f            NUMERIC,            -- NULL means open high end
    yes_token_id    TEXT,
    no_token_id     TEXT,
    condition_id    TEXT,
    tick_size       NUMERIC,
    PRIMARY KEY (event_slug, bucket_label)
);

CREATE TABLE IF NOT EXISTS pm_market_snapshots (
    event_slug      TEXT NOT NULL,
    bucket_label    TEXT NOT NULL,
    snapshot_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    best_bid        NUMERIC,
    best_ask        NUMERIC,
    last_trade      NUMERIC,
    mid             NUMERIC,
    depth_jsonb     JSONB,
    PRIMARY KEY (event_slug, bucket_label, snapshot_at),
    FOREIGN KEY (event_slug, bucket_label) REFERENCES pm_buckets(event_slug, bucket_label) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS pm_market_snapshots_event_time_idx
    ON pm_market_snapshots (event_slug, snapshot_at DESC);

CREATE TABLE IF NOT EXISTS forecasts (
    station_id          INT NOT NULL REFERENCES stations(station_id),
    target_date         DATE NOT NULL,
    source              TEXT NOT NULL,
    run_time            TIMESTAMPTZ NOT NULL,
    predicted_max_f     NUMERIC,
    predicted_std_f     NUMERIC,
    raw                 JSONB,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (station_id, target_date, source, run_time)
);

CREATE INDEX IF NOT EXISTS forecasts_station_date_idx
    ON forecasts (station_id, target_date);

CREATE TABLE IF NOT EXISTS observations (
    station_id          INT NOT NULL REFERENCES stations(station_id),
    obs_date            DATE NOT NULL,
    source              TEXT NOT NULL,
    observed_max_f      NUMERIC,
    finalized           BOOLEAN NOT NULL DEFAULT FALSE,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (station_id, obs_date, source)
);

CREATE INDEX IF NOT EXISTS observations_station_date_idx
    ON observations (station_id, obs_date);

CREATE TABLE IF NOT EXISTS climatology (
    station_id      INT NOT NULL REFERENCES stations(station_id),
    doy             SMALLINT NOT NULL CHECK (doy BETWEEN 1 AND 366),
    tmax_mean       NUMERIC,
    tmax_std        NUMERIC,
    sample_n        INT,
    refreshed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (station_id, doy)
);

CREATE TABLE IF NOT EXISTS predictions (
    model_id        TEXT NOT NULL,
    station_id      INT NOT NULL REFERENCES stations(station_id),
    target_date     DATE NOT NULL,
    run_time        TIMESTAMPTZ NOT NULL,
    mean_f          NUMERIC NOT NULL,
    std_f           NUMERIC NOT NULL,
    features_jsonb  JSONB,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (model_id, station_id, target_date, run_time)
);

CREATE INDEX IF NOT EXISTS predictions_model_date_idx
    ON predictions (model_id, target_date);

CREATE TABLE IF NOT EXISTS bucket_probs (
    model_id        TEXT NOT NULL,
    event_slug      TEXT NOT NULL REFERENCES pm_events(event_slug) ON DELETE CASCADE,
    bucket_label    TEXT NOT NULL,
    run_time        TIMESTAMPTZ NOT NULL,
    prob            NUMERIC NOT NULL CHECK (prob >= 0 AND prob <= 1),
    PRIMARY KEY (model_id, event_slug, bucket_label, run_time),
    FOREIGN KEY (event_slug, bucket_label) REFERENCES pm_buckets(event_slug, bucket_label) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS bucket_probs_event_time_idx
    ON bucket_probs (event_slug, run_time DESC);

CREATE TABLE IF NOT EXISTS calibration_runs (
    run_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_id            TEXT NOT NULL,
    station_id          INT REFERENCES stations(station_id),
    horizon_days        INT,
    sample_n            INT NOT NULL,
    log_loss            NUMERIC,
    brier               NUMERIC,
    reliability_jsonb   JSONB,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at            TIMESTAMPTZ
);
