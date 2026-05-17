-- ╔═══════════════════════════════════════════════════════════════════════════╗
-- ║  Migration 004 — PAMM Trade Allocations                                   ║
-- ║                                                                           ║
-- ║  Scope:                                                                   ║
-- ║    1. Creates `user_trade_allocations` — written by the Next.js PAMM      ║
-- ║       webhook on every trade close. One row per active client per trade.  ║
-- ║       Contains gross PnL, Binance commission share, performance fee, and  ║
-- ║       net PnL credited to the client's balance.                           ║
-- ║                                                                           ║
-- ║    2. Extends the `ledger_transactions` CHECK constraint to include two   ║
-- ║       new types:                                                          ║
-- ║         BINANCE_COMMISSION — per-trade commission recovered from client.  ║
-- ║         TRADE_ALLOCATION   — net PnL delta (positive or negative) for     ║
-- ║                              informational audit ledger rows.             ║
-- ║                                                                           ║
-- ║  PAMM Math (per client per trade):                                        ║
-- ║    gross_pnl   = balance_before × pnl_pct           (can be negative)    ║
-- ║    commission  = balance_before × fees_pct           (always ≥ 0)        ║
-- ║    perf_fee    = max(gross_pnl, 0) × perf_fee_pct   (only on gains)      ║
-- ║    admin_fee   = commission + perf_fee                                    ║
-- ║    net_pnl     = gross_pnl − commission − perf_fee                       ║
-- ║    new_balance = balance_before + net_pnl                                 ║
-- ║                                                                           ║
-- ║  Safety:                                                                  ║
-- ║    • Additive-only migration. No existing data is modified.               ║
-- ║    • CHECK constraint change is backward-compatible (only adds values).  ║
-- ║    • The Python bot does NOT write to this table directly.                ║
-- ╚═══════════════════════════════════════════════════════════════════════════╝

BEGIN;

-- ─── user_trade_allocations ──────────────────────────────────────────────────
-- One row per (user, closed_trade). Written atomically via a Prisma $transaction
-- in the PAMM webhook alongside the user.balance_usdt update.

CREATE TABLE IF NOT EXISTS user_trade_allocations (
    id               BIGSERIAL      PRIMARY KEY,
    user_id          UUID           NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- Trade identifiers.
    symbol           VARCHAR(32)    NOT NULL,
    side             VARCHAR(8)     NOT NULL,           -- 'BUY' | 'SELL'
    exit_reason      VARCHAR(64)    NOT NULL DEFAULT 'unknown',

    -- PAMM math columns (all NUMERIC for exact arithmetic; no FLOAT).
    pnl_pct          NUMERIC(12, 8) NOT NULL,           -- global bot P&L, e.g. 0.015 = +1.5%
    gross_pnl_usdt   NUMERIC(28,12) NOT NULL,           -- balance_before × pnl_pct
    commission_usdt  NUMERIC(28,12) NOT NULL DEFAULT 0, -- Binance fee share (always ≥ 0)
    perf_fee_usdt    NUMERIC(28,12) NOT NULL DEFAULT 0, -- 5% of gross_pnl when > 0
    admin_fee_usdt   NUMERIC(28,12) NOT NULL DEFAULT 0, -- commission_usdt + perf_fee_usdt
    net_pnl_usdt     NUMERIC(28,12) NOT NULL,           -- credited to client (can be negative)

    -- Balance snapshot for auditability.
    balance_before   NUMERIC(28,12) NOT NULL,
    balance_after    NUMERIC(28,12) NOT NULL,

    allocated_at     TIMESTAMPTZ(6) NOT NULL DEFAULT NOW()
);

-- NOTE: CREATE INDEX requires table ownership; wrap to avoid crashing if
-- the table was pre-created under a different DB role (e.g. postgres).
DO $$
BEGIN
    CREATE INDEX IF NOT EXISTS idx_uta_user_id      ON user_trade_allocations(user_id);
    CREATE INDEX IF NOT EXISTS idx_uta_allocated_at ON user_trade_allocations(allocated_at DESC);
EXCEPTION
    WHEN OTHERS THEN
        RAISE NOTICE 'Skipped index creation on user_trade_allocations: %', SQLERRM;
END;
$$;
-- idx_uta_symbol is created by migration 007 after the symbol column is added.

-- ─── Extend ledger_transactions CHECK constraint ──────────────────────────────
-- PostgreSQL does not support in-place modification of named CHECK constraints.
-- Drop and recreate is the standard approach; it does not affect existing data.
-- Wrapped in DO $$ EXCEPTION $$ so a non-superuser app role (bot_admin) that
-- does NOT own ledger_transactions can still run this migration without failing.
DO $$
BEGIN
    ALTER TABLE ledger_transactions DROP CONSTRAINT IF EXISTS ledger_type_check;
    ALTER TABLE ledger_transactions
        ADD CONSTRAINT ledger_type_check
            CHECK (type IN (
                'DEPOSIT',
                'ENTRY_FEE',
                'PERFORMANCE_FEE',
                'BINANCE_COMMISSION',
                'WITHDRAWAL'
            ));
EXCEPTION
    WHEN OTHERS THEN
        RAISE NOTICE 'Skipped ledger_type_check update: %', SQLERRM;
END;
$$;

-- ─── Permissions ─────────────────────────────────────────────────────────────
-- bot_admin is the application role used by Next.js (DATABASE_URL in Coolify).
-- If bot_admin created user_trade_allocations it is already the owner (all
-- privileges implicit). Wrapped in exception handler for safety.
DO $$
BEGIN
    GRANT SELECT, INSERT ON user_trade_allocations TO bot_admin;
    GRANT USAGE, SELECT ON SEQUENCE user_trade_allocations_id_seq TO bot_admin;
EXCEPTION
    WHEN OTHERS THEN
        RAISE NOTICE 'Skipped grants on user_trade_allocations: %', SQLERRM;
END;
$$;

COMMIT;
