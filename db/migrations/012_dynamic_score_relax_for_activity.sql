-- 012_dynamic_score_relax_for_activity.sql
-- Objective:
--   Relax dynamic score floor so the AI sidecar can increase activity in calm
--   without disabling risk controls. Keep upper bound unchanged.
--
-- Changes:
--   score_min_override check constraint: [6.5, 9.2] -> [5.5, 9.2]

BEGIN;

UPDATE dynamic_symbol_config
SET score_min_override = LEAST(9.2, GREATEST(5.5, score_min_override));

ALTER TABLE dynamic_symbol_config
    DROP CONSTRAINT IF EXISTS chk_dsc_score_min_override;

ALTER TABLE dynamic_symbol_config
    ADD CONSTRAINT chk_dsc_score_min_override
    CHECK (score_min_override BETWEEN 5.5 AND 9.2);

COMMIT;
