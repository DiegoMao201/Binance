-- 001_recorder.sql
-- Binance recorder + shadow motor tables.
-- Run as binance_bot against optiferre_binance.

BEGIN;

-- ─── Stream health ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS recorder_health (
    stream_name         TEXT PRIMARY KEY,
    last_msg_at         TIMESTAMPTZ,
    msg_count           BIGINT  NOT NULL DEFAULT 0,
    gap_count           INTEGER NOT NULL DEFAULT 0,
    last_gap_at         TIMESTAMPTZ,
    status              TEXT    NOT NULL DEFAULT 'starting',
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── Liquidations (!forceOrder@arr — throttled 1/s/symbol) ────────────────────
CREATE TABLE IF NOT EXISTS binance_liquidations (
    id                  BIGSERIAL PRIMARY KEY,
    recv_ts_us          BIGINT      NOT NULL,
    ts                  TIMESTAMPTZ NOT NULL,
    symbol              TEXT        NOT NULL,
    side                TEXT        NOT NULL,  -- BUY = long liq, SELL = short liq
    order_type          TEXT        NOT NULL,
    time_in_force       TEXT        NOT NULL,
    original_qty        NUMERIC     NOT NULL,
    price               NUMERIC     NOT NULL,
    avg_price           NUMERIC     NOT NULL,
    order_status        TEXT        NOT NULL,
    last_filled_qty     NUMERIC     NOT NULL,
    acc_filled_qty      NUMERIC     NOT NULL,
    trade_time_ms       BIGINT      NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_liq_ts_sym ON binance_liquidations (ts, symbol);
CREATE INDEX IF NOT EXISTS idx_liq_symbol ON binance_liquidations (symbol, ts);

-- ─── Mark price (futures, 1s per symbol, partitioned monthly) ─────────────────
CREATE TABLE IF NOT EXISTS binance_mark_price (
    recv_ts_us          BIGINT      NOT NULL,
    ts                  TIMESTAMPTZ NOT NULL,
    symbol              TEXT        NOT NULL,
    mark_price          NUMERIC     NOT NULL,
    index_price         NUMERIC     NOT NULL,
    funding_rate        NUMERIC,
    next_funding_time   BIGINT,
    PRIMARY KEY (ts, symbol)
) PARTITION BY RANGE (ts);

CREATE TABLE IF NOT EXISTS binance_mark_price_2026_08
    PARTITION OF binance_mark_price
    FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');

CREATE TABLE IF NOT EXISTS binance_mark_price_2026_09
    PARTITION OF binance_mark_price
    FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');

CREATE TABLE IF NOT EXISTS binance_mark_price_2026_10
    PARTITION OF binance_mark_price
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');

CREATE TABLE IF NOT EXISTS binance_mark_price_2026_11
    PARTITION OF binance_mark_price
    FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');

CREATE TABLE IF NOT EXISTS binance_mark_price_2026_12
    PARTITION OF binance_mark_price
    FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');

-- ─── OI and L/S metrics (REST every 5 min) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS binance_oi_metrics (
    ts                          TIMESTAMPTZ NOT NULL,
    symbol                      TEXT        NOT NULL,
    open_interest               NUMERIC,
    open_interest_value         NUMERIC,
    long_short_ratio_top_pos    NUMERIC,
    long_short_ratio_top_acc    NUMERIC,
    long_short_ratio_global     NUMERIC,
    taker_buy_vol               NUMERIC,
    taker_sell_vol              NUMERIC,
    PRIMARY KEY (ts, symbol)
);
CREATE INDEX IF NOT EXISTS idx_oi_sym_ts ON binance_oi_metrics (symbol, ts);

-- ─── Exchange info cache (daily refresh via REST) ─────────────────────────────
CREATE TABLE IF NOT EXISTS binance_exchange_info_cache (
    ts                          TIMESTAMPTZ NOT NULL,
    symbol                      TEXT        NOT NULL,
    tick_size                   NUMERIC     NOT NULL,
    step_size                   NUMERIC     NOT NULL,
    min_notional                NUMERIC     NOT NULL,
    amend_allowed               BOOLEAN     NOT NULL DEFAULT FALSE,
    peg_instructions_allowed    BOOLEAN     NOT NULL DEFAULT FALSE,
    raw                         JSONB,
    PRIMARY KEY (ts, symbol)
);

-- ─── Order book sequence gaps (validation log) ────────────────────────────────
CREATE TABLE IF NOT EXISTS recorder_book_gaps (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol      TEXT        NOT NULL,
    expected_u  BIGINT      NOT NULL,
    got_U       BIGINT      NOT NULL,
    action      TEXT        NOT NULL DEFAULT 'resync'
);

-- ─── Shadow trades (simulated fills + markout) ────────────────────────────────
CREATE TABLE IF NOT EXISTS shadow_trades (
    id                  BIGSERIAL PRIMARY KEY,
    strategy_name       TEXT        NOT NULL,
    signal_ts           TIMESTAMPTZ NOT NULL,
    fill_ts             TIMESTAMPTZ,
    symbol              TEXT        NOT NULL,
    side                TEXT        NOT NULL,  -- BUY or SELL
    signal_price        NUMERIC     NOT NULL,
    fill_price          NUMERIC,
    qty                 NUMERIC     NOT NULL,
    notional            NUMERIC     NOT NULL,
    queue_pos_initial   NUMERIC,   -- queue depth at submission (pessimistic: full level qty)
    queue_pos_at_fill   NUMERIC,   -- queue remaining when filled (should be ~0)
    status              TEXT        NOT NULL DEFAULT 'PENDING',
    -- PENDING, FILLED, REJECTED_2010, EXPIRED, CANCELLED
    reject_reason       TEXT,
    markout_1s          NUMERIC,   -- sign*(mid_{t+1s} - fill_price)
    markout_5s          NUMERIC,
    markout_30s         NUMERIC,
    markout_60s         NUMERIC,
    sigma_realized_60m  NUMERIC,   -- realized vol at fill time (60m window)
    vpin_bucket         SMALLINT,  -- VPIN quintile [1..5] at fill time
    feature_snapshot    JSONB,     -- full market state at signal_ts, ante la duda guarda más
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_shadow_strat_ts  ON shadow_trades (strategy_name, signal_ts);
CREATE INDEX IF NOT EXISTS idx_shadow_sym_fill  ON shadow_trades (symbol, fill_ts) WHERE fill_ts IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_shadow_status    ON shadow_trades (status);
CREATE INDEX IF NOT EXISTS idx_shadow_markout   ON shadow_trades (strategy_name, fill_ts)
    WHERE fill_ts IS NOT NULL AND markout_60s IS NULL;

-- ─── Shadow equity (running P&L curve per strategy) ──────────────────────────
CREATE TABLE IF NOT EXISTS shadow_equity (
    ts              TIMESTAMPTZ NOT NULL,
    strategy_name   TEXT        NOT NULL,
    equity          NUMERIC     NOT NULL,
    open_positions  INTEGER     NOT NULL DEFAULT 0,
    realized_pnl    NUMERIC     NOT NULL DEFAULT 0,
    unrealized_pnl  NUMERIC     NOT NULL DEFAULT 0,
    trade_count     INTEGER     NOT NULL DEFAULT 0,
    fill_count      INTEGER     NOT NULL DEFAULT 0,
    reject_count    INTEGER     NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, strategy_name)
);

-- ─── Hypothesis registry (append-only; each backtest registered before results) ─
CREATE TABLE IF NOT EXISTS hypotheses (
    id              BIGSERIAL PRIMARY KEY,
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    hypothesis_id   TEXT        NOT NULL UNIQUE,  -- user-assigned slug
    description     TEXT        NOT NULL,
    strategy_params JSONB,
    expected_ev_bps NUMERIC,
    result_ev_bps   NUMERIC,    -- filled after test completes
    result_at       TIMESTAMPTZ,
    dsr_input       JSONB,      -- params for DSR calc (N configs tested so far, etc.)
    passed          BOOLEAN     -- NULL = pending
);

COMMIT;
