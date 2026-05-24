-- 013_dynamic_spike_prefilter_tick_range.sql
-- Extends dynamic tick-domain spike_pre_filter guardrail for 900/1000 symbols.
-- New enforced range: 50..2500 ticks.

BEGIN;

UPDATE dynamic_symbol_config
SET spike_pre_filter_target = LEAST(2500, GREATEST(50, spike_pre_filter_target));

ALTER TABLE dynamic_symbol_config
    DROP CONSTRAINT IF EXISTS chk_dsc_spike_pre_filter_target;

ALTER TABLE dynamic_symbol_config
    ADD CONSTRAINT chk_dsc_spike_pre_filter_target
    CHECK (spike_pre_filter_target BETWEEN 50 AND 2500);

COMMIT;
