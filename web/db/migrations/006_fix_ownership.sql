-- ╔═══════════════════════════════════════════════════════════════════════════╗
-- ║  Migration 006 — Transfer all table/sequence ownership to bot_admin      ║
-- ║                                                                           ║
-- ║  Run as: postgres superuser (POSTGRES_SUPERUSER_URL)                     ║
-- ║                                                                           ║
-- ║  Uses per-item EXCEPTION handlers so the entire DO block succeeds even   ║
-- ║  if individual ALTERs fail (e.g. linked sequences must follow table       ║
-- ║  owner, cannot be altered independently).                                ║
-- ╚═══════════════════════════════════════════════════════════════════════════╝

DO $$
DECLARE
    tbl_name TEXT;
    seq_name TEXT;
BEGIN
    -- ── 1. Transfer ownership of every public table to bot_admin ──────────
    FOR tbl_name IN
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public'
    LOOP
        BEGIN
            EXECUTE format('ALTER TABLE IF EXISTS %I OWNER TO bot_admin', tbl_name);
            RAISE NOTICE 'Table % → bot_admin OK', tbl_name;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'Table % skipped: %', tbl_name, SQLERRM;
        END;
    END LOOP;

    -- ── 2. Transfer ownership of standalone sequences ─────────────────────
    -- Linked sequences (SERIAL/identity) have their owner updated automatically
    -- when the parent table is re-owned above; attempting to ALTER them directly
    -- raises "cannot change owner of sequence" — hence the exception handler.
    FOR seq_name IN
        SELECT sequence_name FROM information_schema.sequences
        WHERE sequence_schema = 'public'
    LOOP
        BEGIN
            EXECUTE format('ALTER SEQUENCE IF EXISTS %I OWNER TO bot_admin', seq_name);
            RAISE NOTICE 'Sequence % → bot_admin OK', seq_name;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'Sequence % skipped (linked): %', seq_name, SQLERRM;
        END;
    END LOOP;
END;
$$;

