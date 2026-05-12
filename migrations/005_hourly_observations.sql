-- Hourly (and sub-hourly where reported) METAR temperatures for primary and
-- optional neighbor ASOS sites. Used for:
--   * higher-frequency hindcasts / nowcast features vs daily GHCN alone
--   * multi-site spatial context around each Polymarket resolution airport
--
-- station_id always references the Polymarket market's canonical station row;
-- site_icao is the actual reporting METAR station (primary ICAO or a neighbor).

CREATE TABLE IF NOT EXISTS hourly_observations (
    station_id    INT NOT NULL REFERENCES stations(station_id),
    obs_ts_utc    TIMESTAMPTZ NOT NULL,
    site_icao     TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'mesonet:asos_hourly',
    temp_f        NUMERIC NOT NULL,
    raw           JSONB,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (station_id, obs_ts_utc, site_icao, source)
);

CREATE INDEX IF NOT EXISTS hourly_obs_station_ts_idx
    ON hourly_observations (station_id, obs_ts_utc DESC);

CREATE INDEX IF NOT EXISTS hourly_obs_site_ts_idx
    ON hourly_observations (site_icao, obs_ts_utc DESC);
