-- ╔═══════════════════════════════════════════════════════════════════════════╗
-- ║  Migration 006 — Deriv Synthetic Indices Storage                          ║
-- ║                                                                           ║
-- ║  Creates two new tables for the Deriv pipeline:                           ║
-- ║    1. deriv_tick_snapshots  — periodic statistical snapshots of each      ║
-- ║       symbol (Hurst, autocorrelation, volatility regime, risk score).     ║
-- ║       Written by deriv_analyst.py every N ticks.                         ║
-- ║    2. deriv_contracts       — one row per closed Deriv contract,          ║
-- ║       including the score/stats at entry time and AI gate result.         ║
-- ║       Provides a queryable alternative to the JSON flat files.            ║
-- ║                                                                           ║
-- ║  These tables expose the full statistical context of every trade so the   ║
-- ║  platform can answer questions like:                                       ║
-- ║    • What is the win rate when Hurst > 0.55?                              ║
-- ║    • Does the AI gate improve or hurt win rate?                           ║
-- ║    • Which volatility regime is most profitable?                          ║
-- ║    • Is there correlation between entry score and realised PnL?           ║
-- ║                                                                           ║
-- ║  PostgREST automatically exposes these tables at /deriv_tick_snapshots    ║
-- ║  and /deriv_contracts for the Next.js frontend and pandas queries.        ║
-- ║                                                                           ║
-- ║  Idempotent — safe to re-run.                                             ║
-- ╚═══════════════════════════════════════════════════════════════════════════╝

BEGIN;

-- ─── 1. TICK SNAPSHOTS ───────────────────────────────────────────────────────
-- Lightweight statistical snapshot captured ~every 30 ticks per symbol.
-- Used for time-series analysis of market regime and scoring history.
CREATE TABLE IF NOT EXISTS deriv_tick_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Tick buffer info
    tick_count      INTEGER NOT NULL,
    last_price      NUMERIC(28, 8) NOT NULL,

    -- Risk engine score
    score           NUMERIC(8, 4),
    regime          VARCHAR(20),   -- 'trending' | 'ranging' | 'volatile' | 'calm'

    -- Hurst exponent — core statistical indicator
    -- >0.5 = persistent trend, 0.5 = random, <0.5 = mean-reverting
    hurst           NUMERIC(8, 6),

    -- Autocorrelation of log-returns at lag 1
    -- Positive = momentum, Negative = reversal tendency
    autocorr_lag1   NUMERIC(8, 6),

    -- Rolling volatility (std of 20-tick returns)
    rolling_vol     NUMERIC(16, 10),

    -- Linear regression on full window
    trend_slope     NUMERIC(16, 10),
    r_squared       NUMERIC(8, 6),

    -- Full score breakdown as JSONB for flexible querying
    score_breakdown JSONB
);

CREATE INDEX IF NOT EXISTS idx_dts_symbol_time
    ON deriv_tick_snapshots (symbol, captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_dts_score
    ON deriv_tick_snapshots (score);

CREATE INDEX IF NOT EXISTS idx_dts_hurst
    ON deriv_tick_snapshots (hurst);


-- ─── 2. DERIV CONTRACTS ──────────────────────────────────────────────────────
-- One row per Deriv multiplier contract (opened + closed pair).
-- This is the primary analytics table for backtesting and ML feature engineering.
CREATE TABLE IF NOT EXISTS deriv_contracts (
    id                  BIGSERIAL PRIMARY KEY,

    -- Deriv identifiers
    contract_id         BIGINT UNIQUE NOT NULL,
    intent_id           UUID,

    -- Trade metadata
    symbol              VARCHAR(20)  NOT NULL,
    side                VARCHAR(16)  NOT NULL,   -- 'MULTUP' | 'MULTDOWN'
    stake_usdt          NUMERIC(28, 8) NOT NULL,
    multiplier          INTEGER NOT NULL,

    -- Prices
    entry_price         NUMERIC(28, 8),
    exit_price          NUMERIC(28, 8),

    -- Timestamps
    opened_at           TIMESTAMPTZ,
    closed_at           TIMESTAMPTZ,
    hold_seconds        INTEGER GENERATED ALWAYS AS (
                            EXTRACT(EPOCH FROM (closed_at - opened_at))::INTEGER
                        ) STORED,

    -- Outcome
    exit_reason         VARCHAR(32),  -- 'take_profit' | 'stop_loss' | 'manual' | 'expired'
    realized_pnl_usdt   NUMERIC(28, 8),
    is_winner           BOOLEAN GENERATED ALWAYS AS (realized_pnl_usdt > 0) STORED,

    -- Statistical context at entry — the full picture of WHY the bot entered
    score_at_entry      NUMERIC(8, 4),
    regime_at_entry     VARCHAR(20),
    hurst_at_entry      NUMERIC(8, 6),
    autocorr_at_entry   NUMERIC(8, 6),
    vol_regime_at_entry VARCHAR(20),  -- 'expanding' | 'compressing' | 'normal'
    rolling_vol_entry   NUMERIC(16, 10),
    trend_slope_entry   NUMERIC(16, 10),
    r_squared_entry     NUMERIC(8, 6),

    -- AI gate result
    ai_approved         BOOLEAN,
    ai_confidence       NUMERIC(5, 4),
    ai_model            VARCHAR(80),
    ai_reason           TEXT,

    -- Full score breakdown at entry (for retrospective analysis)
    score_breakdown     JSONB,

    -- Audit
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dc_symbol_opened
    ON deriv_contracts (symbol, opened_at DESC);

CREATE INDEX IF NOT EXISTS idx_dc_exit_reason
    ON deriv_contracts (exit_reason);

CREATE INDEX IF NOT EXISTS idx_dc_is_winner
    ON deriv_contracts (is_winner);

CREATE INDEX IF NOT EXISTS idx_dc_hurst
    ON deriv_contracts (hurst_at_entry);

CREATE INDEX IF NOT EXISTS idx_dc_score
    ON deriv_contracts (score_at_entry);

CREATE INDEX IF NOT EXISTS idx_dc_regime
    ON deriv_contracts (regime_at_entry);


-- ─── 3. ANALYTICAL VIEWS ─────────────────────────────────────────────────────
-- Pre-built views exposed by PostgREST for quick dashboard queries.

-- Win rate by volatility regime
CREATE OR REPLACE VIEW v_deriv_regime_stats AS
SELECT
    regime_at_entry                                                   AS regime,
    COUNT(*)                                                          AS trades,
    ROUND(AVG(CASE WHEN is_winner THEN 1.0 ELSE 0.0 END)::NUMERIC, 4) AS win_rate,
    ROUND(SUM(realized_pnl_usdt)::NUMERIC, 4)                        AS total_pnl,
    ROUND(AVG(realized_pnl_usdt)::NUMERIC, 6)                        AS avg_pnl,
    ROUND(AVG(score_at_entry)::NUMERIC, 4)                           AS avg_score
FROM deriv_contracts
WHERE closed_at IS NOT NULL
GROUP BY regime_at_entry
ORDER BY total_pnl DESC;

-- Win rate bucketed by score (shows if higher score → better outcome)
CREATE OR REPLACE VIEW v_deriv_score_buckets AS
SELECT
    FLOOR(score_at_entry)::INTEGER                                    AS score_floor,
    COUNT(*)                                                          AS trades,
    ROUND(AVG(CASE WHEN is_winner THEN 1.0 ELSE 0.0 END)::NUMERIC, 4) AS win_rate,
    ROUND(SUM(realized_pnl_usdt)::NUMERIC, 4)                        AS total_pnl
FROM deriv_contracts
WHERE closed_at IS NOT NULL AND score_at_entry IS NOT NULL
GROUP BY score_floor
ORDER BY score_floor;

-- Hurst buckets (proves edge theory: higher Hurst → better win rate?)
CREATE OR REPLACE VIEW v_deriv_hurst_buckets AS
SELECT
    ROUND(hurst_at_entry::NUMERIC, 1)                                 AS hurst_bucket,
    COUNT(*)                                                          AS trades,
    ROUND(AVG(CASE WHEN is_winner THEN 1.0 ELSE 0.0 END)::NUMERIC, 4) AS win_rate,
    ROUND(SUM(realized_pnl_usdt)::NUMERIC, 4)                        AS total_pnl
FROM deriv_contracts
WHERE closed_at IS NOT NULL AND hurst_at_entry IS NOT NULL
GROUP BY hurst_bucket
ORDER BY hurst_bucket;

-- AI gate effectiveness
CREATE OR REPLACE VIEW v_deriv_ai_gate_stats AS
SELECT
    ai_approved,
    ai_model,
    COUNT(*)                                                          AS trades,
    ROUND(AVG(CASE WHEN is_winner THEN 1.0 ELSE 0.0 END)::NUMERIC, 4) AS win_rate,
    ROUND(SUM(realized_pnl_usdt)::NUMERIC, 4)                        AS total_pnl
FROM deriv_contracts
WHERE closed_at IS NOT NULL AND ai_approved IS NOT NULL
GROUP BY ai_approved, ai_model
ORDER BY ai_approved DESC, total_pnl DESC;


-- ─── Grant access to the app role ────────────────────────────────────────────
DO $$
BEGIN
    -- Grant to bot_admin if it exists (created in earlier migrations)
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bot_admin') THEN
        GRANT SELECT, INSERT, UPDATE ON deriv_tick_snapshots TO bot_admin;
        GRANT SELECT, INSERT, UPDATE ON deriv_contracts       TO bot_admin;
        GRANT USAGE, SELECT ON SEQUENCE deriv_tick_snapshots_id_seq TO bot_admin;
        GRANT USAGE, SELECT ON SEQUENCE deriv_contracts_id_seq       TO bot_admin;
        GRANT SELECT ON v_deriv_regime_stats   TO bot_admin;
        GRANT SELECT ON v_deriv_score_buckets  TO bot_admin;
        GRANT SELECT ON v_deriv_hurst_buckets  TO bot_admin;
        GRANT SELECT ON v_deriv_ai_gate_stats  TO bot_admin;
    END IF;
    -- Also grant to 'web_anon' if PostgREST anon role exists
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'web_anon') THEN
        GRANT SELECT ON deriv_tick_snapshots TO web_anon;
        GRANT SELECT ON deriv_contracts       TO web_anon;
        GRANT SELECT ON v_deriv_regime_stats   TO web_anon;
        GRANT SELECT ON v_deriv_score_buckets  TO web_anon;
        GRANT SELECT ON v_deriv_hurst_buckets  TO web_anon;
        GRANT SELECT ON v_deriv_ai_gate_stats  TO web_anon;
    END IF;
END $$;

COMMIT;
