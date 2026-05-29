-- ╔═══════════════════════════════════════════════════════════════════════════╗
-- ║  Migration 015 — Dynamic size_multiplier (Fase D 2026-05-29)             ║
-- ║                                                                           ║
-- ║  Adds a per-symbol size_multiplier knob to dynamic_symbol_config so the  ║
-- ║  orchestrator LLM can dial stake DOWN (cuarentena soft) or UP (high      ║
-- ║  conviction) without flipping is_active.                                 ║
-- ║                                                                           ║
-- ║  Range: 0.10..2.00. Default 1.00 (neutral).                              ║
-- ║                                                                           ║
-- ║  Bot semantics: final_stake = adaptive_stake * size_multiplier, capped   ║
-- ║  by profile_stake_cap. size_multiplier < 1.0 = soft quarantine.          ║
-- ╚═══════════════════════════════════════════════════════════════════════════╝

BEGIN;

ALTER TABLE dynamic_symbol_config
    ADD COLUMN IF NOT EXISTS size_multiplier DOUBLE PRECISION NOT NULL DEFAULT 1.0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_dsc_size_multiplier'
    ) THEN
        ALTER TABLE dynamic_symbol_config
            ADD CONSTRAINT chk_dsc_size_multiplier
            CHECK (size_multiplier BETWEEN 0.10 AND 2.00);
    END IF;
END $$;

-- Seed any existing rows to 1.0 (no-op if column just created with default).
UPDATE dynamic_symbol_config SET size_multiplier = 1.0 WHERE size_multiplier IS NULL;

COMMIT;
