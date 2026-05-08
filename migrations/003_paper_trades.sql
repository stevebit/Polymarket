-- 003_paper_trades.sql
-- Phase 4b paper-trading shadow ledger: every order the orchestrator
-- *would have placed* lands here when running in --paper mode. Settled
-- against ``observations`` once the event resolves so we can read realised
-- PnL without ever exposing capital.

CREATE TABLE IF NOT EXISTS paper_trades (
    paper_trade_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    posted_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_slug          TEXT NOT NULL REFERENCES pm_events(event_slug) ON DELETE CASCADE,
    bucket_label        TEXT NOT NULL,
    target_date         DATE NOT NULL,
    side                TEXT NOT NULL,         -- 'taker_buy', 'taker_sell', 'maker_buy', 'maker_sell'
    token_id            TEXT,                  -- yes_token or no_token (for traceability)
    price               NUMERIC NOT NULL CHECK (price > 0 AND price < 1),
    shares              INT NOT NULL CHECK (shares > 0),
    notional_usd        NUMERIC NOT NULL,
    p_model_at_post     NUMERIC NOT NULL,
    expected_value_usd  NUMERIC NOT NULL,
    fee_per_share       NUMERIC,
    model_id            TEXT NOT NULL,
    model_run_time      TIMESTAMPTZ,
    -- ``filled_at`` and ``realised_label`` are populated by the settle step.
    filled_at           TIMESTAMPTZ,
    realised_label      TEXT,
    realised_pnl_usd    NUMERIC,
    settled_at          TIMESTAMPTZ,
    notes               TEXT,
    FOREIGN KEY (event_slug, bucket_label) REFERENCES pm_buckets(event_slug, bucket_label) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS paper_trades_event_idx
    ON paper_trades (event_slug, target_date);

CREATE INDEX IF NOT EXISTS paper_trades_settle_idx
    ON paper_trades (settled_at NULLS FIRST, target_date);
