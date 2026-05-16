-- ╔═══════════════════════════════════════════════════════════════════════════╗
-- ║  Migration 005 — trade_id idempotency key + broker discriminator         ║
-- ║                                                                           ║
-- ║  Context:                                                                 ║
-- ║    Migration 004 created user_trade_allocations without trade_id or      ║
-- ║    broker. The Prisma schema was later updated to require both fields     ║
-- ║    for idempotent PAMM settlement and multi-broker attribution.           ║
-- ║    ledger_transactions also needs a broker column, and two new type       ║
-- ║    values are required: TRADE_PNL, BINANCE_FEE_REIMBURSEMENT.            ║
-- ║                                                                           ║
-- ║  Scope:                                                                   ║
-- ║    1. Adds trade_id (VARCHAR 128) to user_trade_allocations               ║
-- ║    2. Adds broker   (VARCHAR 20)  to user_trade_allocations               ║
-- ║    3. Adds broker   (VARCHAR 20)  to ledger_transactions                  ║
-- ║    4. Creates unique constraint (trade_id, user_id)                       ║
-- ║    5. Creates supporting indexes                                          ║
-- ║    6. Extends ledger_type_check to include TRADE_PNL,                     ║
-- ║       BINANCE_FEE_REIMBURSEMENT                                           ║
-- ║                                                                           ║
-- ║  Safety:                                                                  ║
-- ║    • All ALTER TABLE use ADD COLUMN IF NOT EXISTS (idempotent).           ║
-- ║    • Backfill UPDATE guards against NULL before NOT NULL is enforced.     ║
-- ║    • All privilege-sensitive DDL wrapped in DO $$ EXCEPTION $$ blocks.   ║
-- ╚═══════════════════════════════════════════════════════════════════════════╝

BEGIN;

-- ─── 1. Add trade_id to user_trade_allocations ────────────────────────────────
-- The webhook was never functional without this column, so the table should be
-- empty. The backfill UPDATE guards against any orphaned rows anyway.
ALTER TABLE user_trade_allocations
    ADD COLUMN IF NOT EXISTS trade_id VARCHAR(128);

UPDATE user_trade_allocations
    SET trade_id = 'legacy:' || id::text
    WHERE trade_id IS NULL;

ALTER TABLE user_trade_allocations
    ALTER COLUMN trade_id SET NOT NULL;

-- ─── 2. Add broker to user_trade_allocations ─────────────────────────────────
-- Default 'binance' ensures backward compatibility for any pre-existing rows.
ALTER TABLE user_trade_allocations
    ADD COLUMN IF NOT EXISTS broker VARCHAR(20) NOT NULL DEFAULT 'binance';

-- ─── 3. Add broker to ledger_transactions ────────────────────────────────────
ALTER TABLE ledger_transactions
    ADD COLUMN IF NOT EXISTS broker VARCHAR(20) NOT NULL DEFAULT 'binance';

-- ─── 4. Unique constraint (trade_id, user_id) ────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_uta_trade_user'
          AND conrelid = 'user_trade_allocations'::regclass
    ) THEN
        ALTER TABLE user_trade_allocations
            ADD CONSTRAINT uq_uta_trade_user UNIQUE (trade_id, user_id);
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'uq_uta_trade_user: %', SQLERRM;
END;
$$;

-- ─── 5. Supporting indexes ────────────────────────────────────────────────────
DO $$
BEGIN
    CREATE INDEX IF NOT EXISTS idx_user_trade_allocations_broker
        ON user_trade_allocations(broker);
    CREATE INDEX IF NOT EXISTS idx_user_trade_allocations_user_broker
        ON user_trade_allocations(user_id, broker);
    CREATE INDEX IF NOT EXISTS idx_ledger_transactions_broker
        ON ledger_transactions(broker);
EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'Skipped broker index creation: insufficient privilege.';
END;
$$;

-- ─── 6. Extend ledger_transactions CHECK constraint ──────────────────────────
-- Adds TRADE_PNL and BINANCE_FEE_REIMBURSEMENT to the allowed type values.
-- Keeps all existing types so historical rows remain valid.
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
                'BINANCE_FEE_REIMBURSEMENT',
                'TRADE_PNL',
                'WITHDRAWAL'
            ));
EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'Skipped ledger_type_check update: insufficient privilege. A superuser must run this step manually.';
END;
$$;

COMMIT;
