-- 005_markout_long_horizons.sql
-- Markout at trading-strategy horizons (5m, 15m, 30m, 60m).
-- The short horizons (1s–60s) answer "is passive quoting profitable?" — no.
-- These horizons answer "does the fill price revert after a cascade entry?" —
-- that is the actual question the project hinges on.
-- NULL while the horizon has not elapsed; never a fallback value.

ALTER TABLE shadow_trades
    ADD COLUMN IF NOT EXISTS markout_5m_bps        NUMERIC,
    ADD COLUMN IF NOT EXISTS markout_15m_bps       NUMERIC,
    ADD COLUMN IF NOT EXISTS markout_30m_bps       NUMERIC,
    ADD COLUMN IF NOT EXISTS markout_60m_bps       NUMERIC,
    ADD COLUMN IF NOT EXISTS adverse_drift_5m_bps  NUMERIC,
    ADD COLUMN IF NOT EXISTS adverse_drift_15m_bps NUMERIC,
    ADD COLUMN IF NOT EXISTS adverse_drift_30m_bps NUMERIC,
    ADD COLUMN IF NOT EXISTS adverse_drift_60m_bps NUMERIC;
