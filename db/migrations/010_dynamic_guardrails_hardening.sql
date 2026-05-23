-- 010_dynamic_guardrails_hardening.sql
-- Hardens dynamic guardrails after live findings:
--   1) score_min_override must stay in [6.5, 8.0]
--   2) zero_peak_grace_sec must stay >= 60 for symbols with confirmed early-exit pain

UPDATE dynamic_symbol_config
SET score_min_override = LEAST(8.0, GREATEST(6.5, score_min_override));

UPDATE dynamic_symbol_config
SET zero_peak_grace_sec = GREATEST(60, zero_peak_grace_sec)
WHERE symbol IN ('BOOM500', 'CRASH500', 'CRASH600');

ALTER TABLE dynamic_symbol_config
    DROP CONSTRAINT IF EXISTS chk_dsc_score_min_override;

ALTER TABLE dynamic_symbol_config
    ADD CONSTRAINT chk_dsc_score_min_override
    CHECK (score_min_override BETWEEN 6.5 AND 8.0);

ALTER TABLE dynamic_symbol_config
    DROP CONSTRAINT IF EXISTS chk_dsc_sensitive_zero_peak_floor;

ALTER TABLE dynamic_symbol_config
    ADD CONSTRAINT chk_dsc_sensitive_zero_peak_floor
    CHECK (
        symbol NOT IN ('BOOM500', 'CRASH500', 'CRASH600')
        OR zero_peak_grace_sec BETWEEN 60 AND 120
    );
