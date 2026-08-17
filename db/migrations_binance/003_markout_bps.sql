-- 003_markout_bps.sql — add bps markout decomposition columns to shadow_trades
--
-- PROMPT_2 BLOQUE B: markout_Xs columns are in raw price units (USD/token).
-- Add parallel bps columns plus mid_at_fill and the adverse-drift decomposition.
-- Old markout_Xs columns are kept intact for backward compatibility.
--
-- markout_h_bps  = half_spread_captured_bps + adverse_drift_h_bps
-- half_spread_captured_bps = 10000 * side_sign * (mid_at_fill - fill_price) / mid_at_fill
-- adverse_drift_h_bps      = 10000 * side_sign * (mid_{t+h}  - mid_at_fill) / mid_at_fill

ALTER TABLE shadow_trades
  ADD COLUMN IF NOT EXISTS mid_at_fill              NUMERIC,
  ADD COLUMN IF NOT EXISTS half_spread_captured_bps NUMERIC,
  ADD COLUMN IF NOT EXISTS markout_1s_bps           NUMERIC,
  ADD COLUMN IF NOT EXISTS markout_5s_bps           NUMERIC,
  ADD COLUMN IF NOT EXISTS markout_30s_bps          NUMERIC,
  ADD COLUMN IF NOT EXISTS markout_60s_bps          NUMERIC,
  ADD COLUMN IF NOT EXISTS adverse_drift_1s_bps     NUMERIC,
  ADD COLUMN IF NOT EXISTS adverse_drift_5s_bps     NUMERIC,
  ADD COLUMN IF NOT EXISTS adverse_drift_30s_bps    NUMERIC,
  ADD COLUMN IF NOT EXISTS adverse_drift_60s_bps    NUMERIC;
