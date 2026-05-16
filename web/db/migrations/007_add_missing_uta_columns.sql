-- ╔═══════════════════════════════════════════════════════════════════════════╗
-- ║  Migration 007 — Add missing columns to user_trade_allocations            ║
-- ║                                                                           ║
-- ║  Problem: the table was created before migration 004 (by Prisma or        ║
-- ║  manually) without the full schema. Migration 004's                       ║
-- ║  CREATE TABLE IF NOT EXISTS was therefore a no-op, leaving the table      ║
-- ║  missing symbol, side, exit_reason, pnl_pct and the monetary columns.     ║
-- ║                                                                           ║
-- ║  This migration adds every column Prisma schema + migration 004 expects,  ║
-- ║  using ADD COLUMN IF NOT EXISTS so it is safe to re-run.                  ║
-- ╚═══════════════════════════════════════════════════════════════════════════╝

BEGIN;

ALTER TABLE user_trade_allocations
    ADD COLUMN IF NOT EXISTS symbol            VARCHAR(32)    NOT NULL DEFAULT 'UNKNOWN',
    ADD COLUMN IF NOT EXISTS side              VARCHAR(8)     NOT NULL DEFAULT 'BUY',
    ADD COLUMN IF NOT EXISTS exit_reason       VARCHAR(64)    NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS pnl_pct           NUMERIC(12, 8) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS gross_pnl_usdt    NUMERIC(28,12) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS commission_usdt   NUMERIC(28,12) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS perf_fee_usdt     NUMERIC(28,12) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS admin_fee_usdt    NUMERIC(28,12) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS net_pnl_usdt      NUMERIC(28,12) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS balance_before    NUMERIC(28,12) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS balance_after     NUMERIC(28,12) NOT NULL DEFAULT 0;

-- Add unique constraint for idempotent webhook (requires trade_id which comes
-- from migration 005 — safe because 005 ran before 007).
DO $$
BEGIN
    ALTER TABLE user_trade_allocations
        ADD CONSTRAINT uq_uta_trade_user UNIQUE (trade_id, user_id);
EXCEPTION
    WHEN duplicate_table THEN NULL;
    WHEN others THEN
        RAISE NOTICE 'Skipped uq_uta_trade_user constraint: %', SQLERRM;
END;
$$;

-- Add indexes for the new columns
DO $$
BEGIN
    CREATE INDEX IF NOT EXISTS idx_uta_symbol ON user_trade_allocations(symbol);
EXCEPTION
    WHEN others THEN
        RAISE NOTICE 'Skipped idx_uta_symbol: %', SQLERRM;
END;
$$;

COMMIT;
