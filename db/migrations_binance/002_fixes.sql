-- 002_fixes.sql
-- Fixes post-review:
--   1. DEFAULT partition for binance_mark_price (prevents crash on unmapped dates)
--   2. binance_mid_snapshots (5s downsampled spot mid — enables markout backfill after restart)
--   3. shadow_order_count (daily order budget tracker for realistic simulation)

BEGIN;

-- ─── 1. DEFAULT partition ──────────────────────────────────────────────────
-- Without this, any INSERT whose ts falls outside 2026-08..2026-12
-- raises "no partition found" and crashes the recorder.
-- The DEFAULT partition catches everything not yet covered.
-- On each new month the recorder creates a named partition and
-- moves the DEFAULT rows via pg_partman or a manual script.
CREATE TABLE IF NOT EXISTS binance_mark_price_default
    PARTITION OF binance_mark_price DEFAULT;

-- ─── 2. Spot mid snapshots (5 s resolution, 6 symbols) ────────────────────
-- Used exclusively for markout backfill after a restart.
-- Volume: 6 symbols × 12 samples/min × 1440 min/day ≈ 103 k rows/day.
-- Older than 2 hours is not needed for markout; keep a rolling window.
CREATE TABLE IF NOT EXISTS binance_mid_snapshots (
    ts          TIMESTAMPTZ NOT NULL,
    symbol      TEXT        NOT NULL,
    mid         NUMERIC     NOT NULL,
    bid         NUMERIC     NOT NULL,
    ask         NUMERIC     NOT NULL,
    PRIMARY KEY (ts, symbol)
);
-- Expire rows older than 2 h via a periodic DELETE in the recorder's rest_poller.
CREATE INDEX IF NOT EXISTS idx_mid_snap_sym_ts ON binance_mid_snapshots (symbol, ts DESC);

-- ─── 3. Order count budget (simulated) ───────────────────────────────────
-- shadow motor tracks orders per UTC day to stay under 160 k/day and 50/10s.
-- Purely informational — does not throttle in v1 but exposes the metric.
CREATE TABLE IF NOT EXISTS shadow_order_budget (
    day             DATE        PRIMARY KEY,
    orders_placed   INTEGER     NOT NULL DEFAULT 0,
    orders_rejected INTEGER     NOT NULL DEFAULT 0,
    max_burst_10s   INTEGER     NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMIT;
