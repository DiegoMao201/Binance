-- 011_ai_pattern_memory_and_score_guardrail.sql
-- Extends score guardrail headroom for stricter filtering and adds
-- persistent pattern memory for winning/losing entry combinations.

UPDATE dynamic_symbol_config
SET score_min_override = LEAST(9.2, GREATEST(6.5, score_min_override));

ALTER TABLE dynamic_symbol_config
    DROP CONSTRAINT IF EXISTS chk_dsc_score_min_override;

ALTER TABLE dynamic_symbol_config
    ADD CONSTRAINT chk_dsc_score_min_override
    CHECK (score_min_override BETWEEN 6.5 AND 9.2);

CREATE TABLE IF NOT EXISTS ai_entry_pattern_memory (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    setup_type TEXT NOT NULL,
    regime TEXT NOT NULL,
    score_bucket NUMERIC(6,2) NOT NULL,
    hurst_bucket NUMERIC(5,2) NOT NULL,
    atr_bucket NUMERIC(8,4) NOT NULL,
    geo_bucket NUMERIC(6,2) NOT NULL,
    sample_trades INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    win_rate NUMERIC(6,2) NOT NULL DEFAULT 0,
    avg_pnl_usdt NUMERIC(14,6) NOT NULL DEFAULT 0,
    last_trade_ts TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (
        symbol,
        side,
        setup_type,
        regime,
        score_bucket,
        hurst_bucket,
        atr_bucket,
        geo_bucket
    )
);

CREATE INDEX IF NOT EXISTS idx_ai_entry_pattern_memory_symbol_updated
    ON ai_entry_pattern_memory(symbol, updated_at DESC);
