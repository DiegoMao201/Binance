-- Migration 007: spike_events table
-- Phase 31: structured logging of BOOM/CRASH spike detections.
-- Every time deriv_risk detects a spike (jump > 3× ATR) a [SPIKE_EVENT] log
-- line is emitted.  This table is the authoritative store for spike audit data.
--
-- Populated by: scripts/ingest_spike_events.py (log parser, runs periodically)
-- OR directly via asyncpg in main_deriv._feed_spike_event() (future).

CREATE TABLE IF NOT EXISTS spike_events (
    id           BIGSERIAL PRIMARY KEY,
    symbol       TEXT        NOT NULL,
    direction    TEXT        NOT NULL CHECK (direction IN ('UP', 'DOWN')),
    jump         NUMERIC(14, 6) NOT NULL,
    atr          NUMERIC(14, 6) NOT NULL,
    ratio        NUMERIC(8, 3)  NOT NULL,
    captured_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Fast lookups for per-symbol spike history and recency checks
CREATE INDEX IF NOT EXISTS idx_spike_events_symbol_ts
    ON spike_events (symbol, captured_at DESC);

-- Partial index to quickly find recent large spikes (ratio > 5)
CREATE INDEX IF NOT EXISTS idx_spike_events_large
    ON spike_events (symbol, captured_at DESC)
    WHERE ratio > 5.0;
