-- 004_fill_event_id.sql
-- 1-second bucket timestamp (Unix epoch seconds) per symbol, shared by all fills
-- that result from the same market event (aggTrade sweep).
-- Used for cluster bootstrap: sample by distinct fill_event_id, not by fill row.
-- Effective sample size = count(DISTINCT fill_event_id), not count(*).

ALTER TABLE shadow_trades
    ADD COLUMN IF NOT EXISTS fill_event_id BIGINT;

COMMENT ON COLUMN shadow_trades.fill_event_id IS
  'Unix epoch seconds at fill time — groups concurrent fills into one market event cluster';
