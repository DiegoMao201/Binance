-- ╔═══════════════════════════════════════════════════════════════════════════╗
-- ║  Migration 009 — Dynamic Symbol Config (AI Runtime Bridge)               ║
-- ║                                                                           ║
-- ║  Hot runtime control table consumed by Deriv bot every few seconds.      ║
-- ║  This decouples strategy thresholds from static code/env values.          ║
-- ║                                                                           ║
-- ║  Guardrails enforced at DB level:                                         ║
-- ║    spike_pre_filter_target: 50..500 ticks                                ║
-- ║    zero_peak_grace_sec:      0..120 seconds                              ║
-- ║    score_min_override:       3.0..10.0                                   ║
-- ║                                                                           ║
-- ║  NOTE: score_min_override is authoritative when is_active=true.          ║
-- ╚═══════════════════════════════════════════════════════════════════════════╝

BEGIN;

CREATE TABLE IF NOT EXISTS dynamic_symbol_config (
    symbol                    VARCHAR(20) PRIMARY KEY,
    market_regime             VARCHAR(20) NOT NULL DEFAULT 'NORMAL',
    spike_pre_filter_target   INTEGER NOT NULL DEFAULT 280,
    zero_peak_grace_sec       INTEGER NOT NULL DEFAULT 0,
    score_min_override        DOUBLE PRECISION NOT NULL DEFAULT 6.0,
    is_active                 BOOLEAN NOT NULL DEFAULT TRUE,
    last_updated              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_dsc_market_regime
        CHECK (market_regime IN ('FAST', 'SLOW', 'NORMAL')),
    CONSTRAINT chk_dsc_spike_pre_filter_target
        CHECK (spike_pre_filter_target BETWEEN 50 AND 500),
    CONSTRAINT chk_dsc_zero_peak_grace_sec
        CHECK (zero_peak_grace_sec BETWEEN 0 AND 120),
    CONSTRAINT chk_dsc_score_min_override
        CHECK (score_min_override BETWEEN 3.0 AND 10.0)
);

CREATE INDEX IF NOT EXISTS idx_dsc_is_active
    ON dynamic_symbol_config (is_active);

CREATE OR REPLACE FUNCTION touch_dynamic_symbol_config_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.last_updated = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_touch_dynamic_symbol_config_updated_at ON dynamic_symbol_config;
CREATE TRIGGER trg_touch_dynamic_symbol_config_updated_at
BEFORE UPDATE ON dynamic_symbol_config
FOR EACH ROW
EXECUTE FUNCTION touch_dynamic_symbol_config_updated_at();

INSERT INTO dynamic_symbol_config (symbol, score_min_override)
VALUES
    ('BOOM1000', 6.5),
    ('BOOM900', 6.5),
    ('BOOM600', 6.5),
    ('BOOM500', 6.5),
    ('CRASH1000', 6.5),
    ('CRASH900', 6.5),
    ('CRASH600', 6.5),
    ('CRASH500', 6.5)
ON CONFLICT (symbol) DO NOTHING;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bot_admin') THEN
        GRANT SELECT, INSERT, UPDATE ON dynamic_symbol_config TO bot_admin;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'web_anon') THEN
        GRANT SELECT ON dynamic_symbol_config TO web_anon;
    END IF;
END $$;

COMMIT;
