-- ╔═══════════════════════════════════════════════════════════════════════════╗
-- ║  Migration 003 — Admin Backoffice: Ledger Transactions                    ║
-- ║                                                                           ║
-- ║  Scope:                                                                   ║
-- ║    Creates `ledger_transactions` — an immutable audit log of every        ║
-- ║    capital movement for the PAMM fund: deposits, entry fees,              ║
-- ║    performance fees, and future withdrawals.                              ║
-- ║                                                                           ║
-- ║  Safety:                                                                  ║
-- ║    • Additive-only migration. No existing tables are modified.            ║
-- ║    • The bot's Python layer does NOT write to this table.                 ║
-- ║      Only the Next.js Admin Backoffice writes here.                       ║
-- ║    • amounts are always stored as positive values; type encodes direction.║
-- ╚═══════════════════════════════════════════════════════════════════════════╝

BEGIN;

-- ─── ledger_transactions ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ledger_transactions (
    id           BIGSERIAL PRIMARY KEY,
    user_id      UUID          NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- 'DEPOSIT'         → client wired in funds
    -- 'ENTRY_FEE'       → 2% onboarding fee retained by admin
    -- 'PERFORMANCE_FEE' → 5% of positive trade PnL retained by admin
    -- 'WITHDRAWAL'      → future: client withdraws capital
    type         VARCHAR(32)   NOT NULL,

    -- Always positive. Direction is encoded in `type`.
    amount_usdt  NUMERIC(28,12) NOT NULL
        CONSTRAINT ledger_amount_positive CHECK (amount_usdt > 0),

    description  VARCHAR(255),
    created_at   TIMESTAMPTZ(6) NOT NULL DEFAULT NOW(),

    CONSTRAINT ledger_type_check
        CHECK (type IN ('DEPOSIT', 'ENTRY_FEE', 'PERFORMANCE_FEE', 'WITHDRAWAL'))
);

-- ─── Indexes ──────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_ledger_user_id ON ledger_transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_ledger_type    ON ledger_transactions(type);

-- ─── Permissions ─────────────────────────────────────────────────────────────
-- bot_admin is the application role used by Next.js (DATABASE_URL).
-- Write access is intentionally limited: admin backoffice creates DEPOSIT and
-- ENTRY_FEE rows; PERFORMANCE_FEE rows will be inserted via a future job.
GRANT SELECT, INSERT ON ledger_transactions TO bot_admin;
GRANT USAGE, SELECT ON SEQUENCE ledger_transactions_id_seq TO bot_admin;

COMMIT;
