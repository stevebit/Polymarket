-- 004_orders_fills.sql
-- Phase 5b automation state: signed flow under
-- ``polymarket_weather/automation/``. ``orders`` and ``fills`` are
-- reconciled with the live CLOB on every orchestrator tick.
--
-- Statuses (str, mirrors py-clob-client-v2 where possible):
--   open / partially_filled / filled / cancelled / rejected / unknown

CREATE TABLE IF NOT EXISTS orders (
    order_id            TEXT PRIMARY KEY,           -- exchange-assigned id
    client_order_id     TEXT,                       -- our local UUID
    posted_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_slug          TEXT NOT NULL REFERENCES pm_events(event_slug),
    bucket_label        TEXT NOT NULL,
    target_date         DATE NOT NULL,
    side                TEXT NOT NULL,
    token_id            TEXT NOT NULL,
    price               NUMERIC NOT NULL,
    requested_shares    INT NOT NULL,
    filled_shares       INT NOT NULL DEFAULT 0,
    cancelled_shares    INT NOT NULL DEFAULT 0,
    status              TEXT NOT NULL,
    p_model_at_post     NUMERIC,
    expected_value_usd  NUMERIC,
    model_id            TEXT,
    model_run_time      TIMESTAMPTZ,
    notes               TEXT,
    raw                 JSONB,
    FOREIGN KEY (event_slug, bucket_label) REFERENCES pm_buckets(event_slug, bucket_label)
);

CREATE INDEX IF NOT EXISTS orders_event_idx ON orders (event_slug, target_date);
CREATE INDEX IF NOT EXISTS orders_status_idx ON orders (status, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS fills (
    fill_id             TEXT PRIMARY KEY,           -- exchange-assigned id
    order_id            TEXT NOT NULL REFERENCES orders(order_id),
    filled_at           TIMESTAMPTZ NOT NULL,
    side                TEXT NOT NULL,
    token_id            TEXT NOT NULL,
    price               NUMERIC NOT NULL,
    shares              INT NOT NULL,
    fee_usd             NUMERIC NOT NULL DEFAULT 0,
    raw                 JSONB,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fills_order_idx ON fills (order_id);
CREATE INDEX IF NOT EXISTS fills_filled_at_idx ON fills (filled_at DESC);

CREATE TABLE IF NOT EXISTS daily_pnl (
    pnl_date            DATE PRIMARY KEY,
    notional_traded_usd NUMERIC NOT NULL DEFAULT 0,
    realised_pnl_usd    NUMERIC NOT NULL DEFAULT 0,
    fees_paid_usd       NUMERIC NOT NULL DEFAULT 0,
    rewards_earned_usd  NUMERIC NOT NULL DEFAULT 0,
    open_orders_count   INT NOT NULL DEFAULT 0,
    fills_count         INT NOT NULL DEFAULT 0,
    refreshed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
