-- ╔═══════════════════════════════════════════════════════════════════════════╗
-- ║  Migration 006 — Transfer table ownership to bot_admin                   ║
-- ║                                                                           ║
-- ║  Context:                                                                 ║
-- ║    Tables user_trade_allocations and ledger_transactions were created     ║
-- ║    by a superuser (postgres). bot_admin cannot ALTER these tables.        ║
-- ║    This migration must be run as the postgres superuser via               ║
-- ║    POSTGRES_SUPERUSER_URL env var.                                        ║
-- ║                                                                           ║
-- ║  Effect:                                                                  ║
-- ║    After this migration, bot_admin owns both tables and can run           ║
-- ║    migration 005 to add trade_id and broker columns.                      ║
-- ╚═══════════════════════════════════════════════════════════════════════════╝

ALTER TABLE IF EXISTS user_trade_allocations OWNER TO bot_admin;
ALTER TABLE IF EXISTS ledger_transactions OWNER TO bot_admin;
ALTER TABLE IF EXISTS users OWNER TO bot_admin;
ALTER TABLE IF EXISTS otp_codes OWNER TO bot_admin;
ALTER TABLE IF EXISTS _prisma_migrations OWNER TO bot_admin;

-- Also grant sequences (for BIGSERIAL columns)
DO $$
DECLARE
    seq_name TEXT;
BEGIN
    FOR seq_name IN
        SELECT sequence_name FROM information_schema.sequences
        WHERE sequence_schema = 'public'
    LOOP
        EXECUTE format('ALTER SEQUENCE %I OWNER TO bot_admin', seq_name);
    END LOOP;
END;
$$;
