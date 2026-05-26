-- 014_dynamic_exit_policy_dep.sql
-- Adds Dynamic Exit Policy (DEP) controls per symbol.
-- Safe rollout defaults keep behavior unchanged:
--   dep_exit_policy='PASSIVE' (no extra closes)

BEGIN;

ALTER TABLE dynamic_symbol_config
    ADD COLUMN IF NOT EXISTS dep_exit_policy TEXT NOT NULL DEFAULT 'PASSIVE';

ALTER TABLE dynamic_symbol_config
    ADD COLUMN IF NOT EXISTS dep_min_hold_sec INTEGER NOT NULL DEFAULT 120;

ALTER TABLE dynamic_symbol_config
    ADD COLUMN IF NOT EXISTS dep_atr_decay_ratio DOUBLE PRECISION NOT NULL DEFAULT 0.70;

ALTER TABLE dynamic_symbol_config
    ADD COLUMN IF NOT EXISTS dep_loss_floor_usdt DOUBLE PRECISION NOT NULL DEFAULT -0.05;

UPDATE dynamic_symbol_config
SET
    dep_exit_policy = CASE
        WHEN UPPER(dep_exit_policy) IN ('PASSIVE', 'SHADOW', 'ACTIVE_DECAY')
            THEN UPPER(dep_exit_policy)
        ELSE 'PASSIVE'
    END,
    dep_min_hold_sec = LEAST(900, GREATEST(30, dep_min_hold_sec)),
    dep_atr_decay_ratio = LEAST(0.95, GREATEST(0.30, dep_atr_decay_ratio)),
    dep_loss_floor_usdt = LEAST(0.0, GREATEST(-5.0, dep_loss_floor_usdt));

ALTER TABLE dynamic_symbol_config
    DROP CONSTRAINT IF EXISTS chk_dsc_dep_exit_policy;

ALTER TABLE dynamic_symbol_config
    ADD CONSTRAINT chk_dsc_dep_exit_policy
    CHECK (dep_exit_policy IN ('PASSIVE', 'SHADOW', 'ACTIVE_DECAY'));

ALTER TABLE dynamic_symbol_config
    DROP CONSTRAINT IF EXISTS chk_dsc_dep_min_hold_sec;

ALTER TABLE dynamic_symbol_config
    ADD CONSTRAINT chk_dsc_dep_min_hold_sec
    CHECK (dep_min_hold_sec BETWEEN 30 AND 900);

ALTER TABLE dynamic_symbol_config
    DROP CONSTRAINT IF EXISTS chk_dsc_dep_atr_decay_ratio;

ALTER TABLE dynamic_symbol_config
    ADD CONSTRAINT chk_dsc_dep_atr_decay_ratio
    CHECK (dep_atr_decay_ratio BETWEEN 0.30 AND 0.95);

ALTER TABLE dynamic_symbol_config
    DROP CONSTRAINT IF EXISTS chk_dsc_dep_loss_floor_usdt;

ALTER TABLE dynamic_symbol_config
    ADD CONSTRAINT chk_dsc_dep_loss_floor_usdt
    CHECK (dep_loss_floor_usdt BETWEEN -5.0 AND 0.0);

COMMIT;
