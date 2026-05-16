-- ╔═══════════════════════════════════════════════════════════════════════════╗
-- ║  Migration 005 — Multi-Broker Discriminator                               ║
-- ║                                                                           ║
-- ║  Adds a `broker` column to every operational table so the platform can    ║
-- ║  hold positions, allocations and audit-trail entries from more than one   ║
-- ║  upstream broker simultaneously without leaking state across pipelines.   ║
-- ║                                                                           ║
-- ║  Default value 'binance' is applied retro-actively to historical rows,    ║
-- ║  guaranteeing 100 % backwards compatibility with all existing queries.    ║
-- ║                                                                           ║
-- ║  Allowed brokers (CHECK constraint):                                      ║
-- ║    'binance' | 'deriv'                                                    ║
-- ║                                                                           ║
-- ║  Idempotent — safe to re-run.                                             ║
-- ╚═══════════════════════════════════════════════════════════════════════════╝

BEGIN;

-- ─── master_trades ───────────────────────────────────────────────────────────
ALTER TABLE master_trades
    ADD COLUMN IF NOT EXISTS broker VARCHAR(20) NOT NULL DEFAULT 'binance';

-- Constraint added separately so it can be re-applied if the column already exists.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_master_trades_broker'
    ) THEN
        ALTER TABLE master_trades
            ADD CONSTRAINT chk_master_trades_broker
            CHECK (broker IN ('binance', 'deriv'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_master_trades_broker
    ON master_trades (broker);


-- ─── user_trade_allocations ──────────────────────────────────────────────────
ALTER TABLE user_trade_allocations
    ADD COLUMN IF NOT EXISTS broker VARCHAR(20) NOT NULL DEFAULT 'binance';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_user_trade_allocations_broker'
    ) THEN
        ALTER TABLE user_trade_allocations
            ADD CONSTRAINT chk_user_trade_allocations_broker
            CHECK (broker IN ('binance', 'deriv'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_user_trade_allocations_broker
    ON user_trade_allocations (broker);

CREATE INDEX IF NOT EXISTS idx_user_trade_allocations_user_broker
    ON user_trade_allocations (user_id, broker);


-- ─── ledger_transactions ─────────────────────────────────────────────────────
-- Optional discriminator: lets us trace which broker generated a fee/PnL row.
ALTER TABLE ledger_transactions
    ADD COLUMN IF NOT EXISTS broker VARCHAR(20) NOT NULL DEFAULT 'binance';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_ledger_transactions_broker'
    ) THEN
        ALTER TABLE ledger_transactions
            ADD CONSTRAINT chk_ledger_transactions_broker
            CHECK (broker IN ('binance', 'deriv'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ledger_transactions_broker
    ON ledger_transactions (broker);


-- ─── pool_ledger ─────────────────────────────────────────────────────────────
-- Pool deposits/withdrawals stay broker-agnostic by design (single USDT pool),
-- but we still tag them so per-broker P&L attribution remains exact.
ALTER TABLE pool_ledger
    ADD COLUMN IF NOT EXISTS broker VARCHAR(20) NOT NULL DEFAULT 'binance';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_pool_ledger_broker'
    ) THEN
        ALTER TABLE pool_ledger
            ADD CONSTRAINT chk_pool_ledger_broker
            CHECK (broker IN ('binance', 'deriv'));
    END IF;
END $$;


-- ─── Backfill ────────────────────────────────────────────────────────────────
-- All historical rows are guaranteed to have broker='binance' (DEFAULT applied
-- automatically on ALTER TABLE … ADD COLUMN … DEFAULT in PostgreSQL ≥ 11),
-- but we run an explicit UPDATE for older Postgres versions and audit clarity.
UPDATE master_trades            SET broker = 'binance' WHERE broker IS NULL OR broker = '';
UPDATE user_trade_allocations   SET broker = 'binance' WHERE broker IS NULL OR broker = '';
UPDATE ledger_transactions      SET broker = 'binance' WHERE broker IS NULL OR broker = '';
UPDATE pool_ledger              SET broker = 'binance' WHERE broker IS NULL OR broker = '';

COMMIT;
