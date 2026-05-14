-- ╔═══════════════════════════════════════════════════════════════════════════╗
-- ║  OptiFerre Terminal — Multi-Tenant PAMM Schema (PostgreSQL ≥ 14)         ║
-- ║                                                                           ║
-- ║  Architecture:                                                            ║
-- ║    • One real Binance "Master" account, executed by the bot.              ║
-- ║    • N investor users hold a *fractional claim* on the Master pool.       ║
-- ║    • Every Master trade is allocated proportionally to active users at    ║
-- ║      the moment of execution (PAMM model).                                ║
-- ║    • Each user pays a SaaS performance fee on profitable allocations.     ║
-- ║      That fee accrues to the platform admin as the "spread" — the bot's  ║
-- ║      gross PnL on Binance is paid out as user_net_pnl + admin_fee_pnl.   ║
-- ║                                                                           ║
-- ║  Precision:                                                               ║
-- ║    All monetary fields use NUMERIC(28, 12)                                ║
-- ║    All ratio/percent fields use NUMERIC(12, 8)                            ║
-- ║    NEVER FLOAT/REAL — fractional allocation arithmetic must be exact.     ║
-- ║                                                                           ║
-- ║  Idempotency:                                                             ║
-- ║    master_trades.exchange_order_id is UNIQUE → upserts safe.              ║
-- ║    user_trade_allocations (master_trade_id, user_id) is the natural PK.   ║
-- ╚═══════════════════════════════════════════════════════════════════════════╝

BEGIN;

-- For UUID generation (Postgres ≥ 13: pgcrypto).
CREATE EXTENSION IF NOT EXISTS "pgcrypto";


-- ─── 1. USERS ────────────────────────────────────────────────────────────────
-- Authentication + the user's CURRENT total balance in the Master pool.
-- balance_usdt is a denormalised cache for fast dashboard reads;  the source
-- of truth is SUM(pool_ledger.amount_usdt) - SUM(user_trade_allocations.user_net_pnl_usdt).
CREATE TABLE IF NOT EXISTS users (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email               VARCHAR(320) UNIQUE NOT NULL,
    password_hash       TEXT NOT NULL,
    display_name        VARCHAR(120),
    role                VARCHAR(16) NOT NULL DEFAULT 'investor'
                        CHECK (role IN ('investor', 'admin')),
    -- Cached balance — updated by the allocator after each trade close.
    balance_usdt        NUMERIC(28, 12) NOT NULL DEFAULT 0,
    -- Fee schedule per user (allows VIP/promo tiers).
    -- 0.20 = 20 % performance fee on positive allocations.
    performance_fee_pct NUMERIC(12, 8) NOT NULL DEFAULT 0.20
                        CHECK (performance_fee_pct >= 0 AND performance_fee_pct <= 1),
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_users_active ON users (is_active) WHERE is_active = TRUE;


-- ─── 2. POOL LEDGER ──────────────────────────────────────────────────────────
-- Append-only, immutable record of every USDT movement between a user and
-- the Master pool (deposit, withdrawal, manual adjustment).
-- This is the source-of-truth for "how much capital did each user contribute".
CREATE TABLE IF NOT EXISTS pool_ledger (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    direction       VARCHAR(16) NOT NULL
                    CHECK (direction IN ('deposit', 'withdrawal', 'adjustment', 'fee_payout')),
    amount_usdt     NUMERIC(28, 12) NOT NULL CHECK (amount_usdt > 0),
    -- Pool snapshot at the time of the movement, used by the allocator to
    -- compute the user's *share* of the Master pool going forward.
    pool_total_before_usdt NUMERIC(28, 12) NOT NULL,
    pool_total_after_usdt  NUMERIC(28, 12) NOT NULL,
    -- External payment reference (Binance Pay tx id, internal voucher, etc.)
    external_ref    VARCHAR(120),
    notes           TEXT,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pool_ledger_user ON pool_ledger (user_id, occurred_at DESC);


-- ─── 3. MASTER TRADES ────────────────────────────────────────────────────────
-- The actual Binance executions.  One row per round-trip (open + close).
-- These are the rows audited by scripts/reconstruct_ledger.py.
CREATE TABLE IF NOT EXISTS master_trades (
    id                  BIGSERIAL PRIMARY KEY,
    -- Binance order id of the closing fill (UNIQUE → makes the audit idempotent).
    exchange_order_id   VARCHAR(64) UNIQUE,
    symbol              VARCHAR(32) NOT NULL,
    side                VARCHAR(8)  NOT NULL CHECK (side IN ('buy', 'sell')),
    -- Open-side facts.
    opened_at           TIMESTAMPTZ NOT NULL,
    entry_price         NUMERIC(28, 12) NOT NULL,
    -- Close-side facts.
    closed_at           TIMESTAMPTZ NOT NULL,
    exit_price          NUMERIC(28, 12) NOT NULL,
    amount_base         NUMERIC(28, 12) NOT NULL,
    notional_quote      NUMERIC(28, 12) NOT NULL,
    -- PnL accounting.
    gross_pnl_usdt      NUMERIC(28, 12) NOT NULL,
    fees_usdt           NUMERIC(28, 12) NOT NULL DEFAULT 0,
    net_pnl_usdt        NUMERIC(28, 12) NOT NULL,
    -- Exit reason from the deductor (take_profit | stop_loss | trailing_stop_tier_N | …)
    exit_reason         VARCHAR(64),
    -- AI / cohort tags (carried through to allocations for analytics joins).
    ai_prompt_version   VARCHAR(16),
    ai_regime           VARCHAR(16),
    ai_micro_gate_path  VARCHAR(32),
    -- Pool snapshot at the moment the trade closed — used by the allocator.
    pool_total_at_close_usdt NUMERIC(28, 12) NOT NULL,
    -- Audit trail.
    ledger_audited_at        TIMESTAMPTZ,
    ledger_pnl_delta_usdt    NUMERIC(28, 12),
    raw_payload         JSONB,  -- full trade dict for forensic replay
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_master_trades_closed ON master_trades (closed_at DESC);
CREATE INDEX IF NOT EXISTS idx_master_trades_symbol ON master_trades (symbol, closed_at DESC);


-- ─── 4. USER TRADE ALLOCATIONS (PIVOT) ───────────────────────────────────────
-- Fractional share of every Master trade for each user that was active at
-- the moment of execution.  This is the table the user-facing dashboard reads.
--
-- Allocation invariant:
--     SUM(allocation_share_pct) over a master_trade ≈ 1.0  (within 1e-9 dust)
--
-- Fee invariant (profit splits):
--     gross_user_pnl  = master_trade.net_pnl_usdt × allocation_share_pct
--     admin_fee_usdt  = max(0, gross_user_pnl) × user.performance_fee_pct
--     user_net_pnl    = gross_user_pnl - admin_fee_usdt
--
-- → On a profitable trade the admin pockets the spread (admin_fee_usdt)
--   while the user receives the post-fee profit.
-- → On a losing trade the loss passes through 1:1 (no fee on losses).
CREATE TABLE IF NOT EXISTS user_trade_allocations (
    id                          BIGSERIAL PRIMARY KEY,
    master_trade_id             BIGINT NOT NULL REFERENCES master_trades(id) ON DELETE CASCADE,
    user_id                     UUID    NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    -- User's share of the Master pool at the moment the trade closed (0..1).
    allocation_share_pct        NUMERIC(12, 8) NOT NULL
                                CHECK (allocation_share_pct >= 0 AND allocation_share_pct <= 1),
    -- Capital fraction the user had at risk (denormalised for fast joins).
    user_capital_at_risk_usdt   NUMERIC(28, 12) NOT NULL,
    -- Raw fractional PnL before fee.
    gross_user_pnl_usdt         NUMERIC(28, 12) NOT NULL,
    -- SaaS performance fee withheld by admin (always >= 0; 0 on losing trades).
    admin_fee_usdt              NUMERIC(28, 12) NOT NULL DEFAULT 0
                                CHECK (admin_fee_usdt >= 0),
    -- Performance fee rate snapshot (in case the user's tier changes later).
    performance_fee_pct_applied NUMERIC(12, 8) NOT NULL,
    -- What the user actually receives in their pool balance.
    user_net_pnl_usdt           NUMERIC(28, 12) NOT NULL,
    allocated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (master_trade_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_alloc_user      ON user_trade_allocations (user_id, allocated_at DESC);
CREATE INDEX IF NOT EXISTS idx_alloc_master    ON user_trade_allocations (master_trade_id);


-- ─── 5. ADMIN REVENUE VIEW ───────────────────────────────────────────────────
-- Convenience view: total spread the platform has accrued.
CREATE OR REPLACE VIEW v_admin_revenue AS
SELECT
    DATE_TRUNC('day', allocated_at) AS day,
    SUM(admin_fee_usdt)             AS admin_revenue_usdt,
    COUNT(*)                        AS n_allocations,
    COUNT(DISTINCT user_id)         AS unique_users
FROM user_trade_allocations
GROUP BY DATE_TRUNC('day', allocated_at)
ORDER BY day DESC;


-- ─── 6. COHORT ANALYTICS VIEW ────────────────────────────────────────────────
-- Mirrors web/app/api/cohort-analytics so the SaaS dashboard can read straight
-- from the DB once the JSON files are migrated.
CREATE OR REPLACE VIEW v_cohort_v3 AS
SELECT
    ai_prompt_version,
    ai_regime,
    ai_micro_gate_path,
    COUNT(*) FILTER (WHERE net_pnl_usdt > 0)::NUMERIC
        / NULLIF(COUNT(*), 0)               AS win_rate,
    SUM(net_pnl_usdt)                       AS total_net_pnl_usdt,
    AVG(net_pnl_usdt)                       AS avg_net_pnl_usdt,
    COUNT(*)                                AS n_trades
FROM master_trades
WHERE ai_prompt_version = 'v3'
GROUP BY ai_prompt_version, ai_regime, ai_micro_gate_path;


COMMIT;

-- ╔═══════════════════════════════════════════════════════════════════════════╗
-- ║  ALLOCATION ALGORITHM (executed in Python after every closed master trade)║
-- ║                                                                           ║
-- ║  1. INSERT INTO master_trades  (idempotent on exchange_order_id).         ║
-- ║  2. SELECT id, balance_usdt, performance_fee_pct                          ║
-- ║       FROM users WHERE is_active = TRUE AND balance_usdt > 0;             ║
-- ║  3. pool_total = SUM(balance_usdt)                                        ║
-- ║     For each user u:                                                      ║
-- ║         share        = u.balance_usdt / pool_total                        ║
-- ║         gross_pnl    = master_trade.net_pnl_usdt * share                  ║
-- ║         admin_fee    = MAX(0, gross_pnl) * u.performance_fee_pct          ║
-- ║         user_net_pnl = gross_pnl - admin_fee                              ║
-- ║         INSERT INTO user_trade_allocations (...);                         ║
-- ║         UPDATE users SET balance_usdt = balance_usdt + user_net_pnl       ║
-- ║                          WHERE id = u.id;                                 ║
-- ║                                                                           ║
-- ║  4. Wrap steps 1-3 in a SERIALIZABLE transaction so concurrent deposits   ║
-- ║     cannot race the allocator.                                            ║
-- ║                                                                           ║
-- ║  5. The admin's spread is the running SUM(admin_fee_usdt) — visible in    ║
-- ║     v_admin_revenue.  No separate "admin balance" row is needed; the     ║
-- ║     fee never enters the user pool, so it accrues outside the system.     ║
-- ╚═══════════════════════════════════════════════════════════════════════════╝
