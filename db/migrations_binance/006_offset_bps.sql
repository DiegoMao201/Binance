-- Migration 006: Add offset_bps to shadow_trades
-- Records the distance in bps between mid at cascade detection and order price.
-- NULL = at-market strategy (baseline_random). Populated by cascade_shadow and baseline_ladder.

ALTER TABLE shadow_trades
    ADD COLUMN IF NOT EXISTS offset_bps INTEGER;

COMMENT ON COLUMN shadow_trades.offset_bps IS
  'Distance in bps from mid_at_detection to order price. 25/50/100/200 for ladder strategies, NULL for at-market.';
