-- ╔═══════════════════════════════════════════════════════════════════════════╗
-- ║  Migration 001 — V3 Cohort Analytics Views                               ║
-- ║                                                                           ║
-- ║  Creates two analytics views on top of master_trades:                    ║
-- ║    v_cohort_v3_metrics   — single-row aggregate of all V3 KPIs           ║
-- ║    v_cohort_v3_breakdown — per-dimension breakdown rows                  ║
-- ║                                                                           ║
-- ║  FILTER (strict — both conditions required):                             ║
-- ║    exchange_order_id IS NOT NULL  →  excludes archived (no_order_id)     ║
-- ║    ledger_audited_at  IS NOT NULL →  excludes unreconciled / corrupted   ║
-- ║                                                                           ║
-- ║  PRECISION:  all monetary arithmetic stays NUMERIC throughout.           ║
-- ║              FLOAT is never used — see schema.sql constraint note.       ║
-- ║                                                                           ║
-- ║  Run:  psql $DATABASE_URL -f db/migrations/001_v_cohort_v3_metrics.sql  ║
-- ╚═══════════════════════════════════════════════════════════════════════════╝

BEGIN;

-- ─── V_COHORT_V3_METRICS ─────────────────────────────────────────────────────
-- Single row.  All five KPIs are computed in one pass over the filtered set.
-- Safe for N=0 — every division is guarded by NULLIF / CASE.
CREATE OR REPLACE VIEW v_cohort_v3_metrics AS
WITH base AS (
    -- ── Strict V3 filter ──────────────────────────────────────────────────
    -- ai_prompt_version = 'v3'         → tag-based cohort boundary
    -- exchange_order_id IS NOT NULL    → real Binance execution (not archived)
    -- ledger_audited_at IS NOT NULL    → reconciled by reconstruct_ledger.py
    SELECT
        net_pnl_usdt,
        fees_usdt,
        (net_pnl_usdt > 0) AS is_win
    FROM  master_trades
    WHERE ai_prompt_version  = 'v3'
      AND exchange_order_id  IS NOT NULL
      AND ledger_audited_at  IS NOT NULL
),
agg AS (
    -- ── Single-pass aggregate (avoids multiple scans of base) ─────────────
    SELECT
        COUNT(*)::NUMERIC                                                AS n_trades,
        COUNT(*) FILTER (WHERE     is_win)::NUMERIC                     AS n_wins,
        COUNT(*) FILTER (WHERE NOT is_win)::NUMERIC                     AS n_losses,
        -- gross_wins:   sum of positive net_pnl rows only
        COALESCE(SUM(net_pnl_usdt)      FILTER (WHERE     is_win), 0)  AS gross_wins_usdt,
        -- gross_losses: sum of ABS(negative net_pnl) — always positive
        COALESCE(SUM(ABS(net_pnl_usdt)) FILTER (WHERE NOT is_win), 0)  AS gross_losses_usdt,
        COALESCE(SUM(net_pnl_usdt),  0)                                 AS total_net_pnl_usdt,
        COALESCE(SUM(fees_usdt),     0)                                 AS total_fees_usdt,
        -- AVG only over winning / losing subsets — NULL when subset is empty
        AVG(net_pnl_usdt)      FILTER (WHERE     is_win)               AS avg_win_usdt,
        AVG(ABS(net_pnl_usdt)) FILTER (WHERE NOT is_win)               AS avg_loss_usdt
    FROM base
)
SELECT
    n_trades,
    n_wins,
    n_losses,

    -- ── KPI 1: Win Rate ───────────────────────────────────────────────────
    -- (n_wins / n_trades) * 100
    -- Returns NULL when no trades exist; NUMERIC(12,8) precision.
    CASE
        WHEN n_trades > 0
        THEN ROUND((n_wins / n_trades) * 100, 8)
        ELSE NULL::NUMERIC
    END                                                AS win_rate_pct,

    -- ── KPI 2: Profit Factor ─────────────────────────────────────────────
    -- ABS( gross_wins / gross_losses )
    -- Edge cases:
    --   n_trades  = 0          → NULL
    --   gross_losses = 0 + gross_wins > 0  → return gross_wins (perfect record)
    --   gross_losses = 0 + gross_wins = 0  → NULL
    CASE
        WHEN n_trades          = 0 THEN NULL::NUMERIC
        WHEN gross_losses_usdt > 0 THEN ROUND(gross_wins_usdt / gross_losses_usdt, 8)
        WHEN gross_wins_usdt   > 0 THEN gross_wins_usdt   -- no losses at all
        ELSE NULL::NUMERIC
    END                                                AS profit_factor,

    -- ── KPI 3: Expected Value per Trade ──────────────────────────────────
    -- SUM(net_pnl) / n_trades
    CASE
        WHEN n_trades > 0
        THEN ROUND(total_net_pnl_usdt / n_trades, 12)
        ELSE NULL::NUMERIC
    END                                                AS ev_per_trade_usdt,

    -- ── KPI 4 & 5: Average Win / Average Loss ────────────────────────────
    -- NULL when no trades in that subset.
    -- Fees already deducted in net_pnl_usdt (see schema invariant).
    avg_win_usdt,
    avg_loss_usdt,

    -- ── Totals (convenience columns for the service layer) ───────────────
    total_net_pnl_usdt,
    gross_wins_usdt,
    gross_losses_usdt,
    total_fees_usdt

FROM agg;


-- ─── V_COHORT_V3_BREAKDOWN ───────────────────────────────────────────────────
-- Multi-row. UNION ALL across three dimensions: regime, gate path, exit reason.
-- The Python service pivots these rows into the nested dict the frontend needs.
-- Each row is independently safe against N=0 per bucket (NULLIF denominator).
CREATE OR REPLACE VIEW v_cohort_v3_breakdown AS

-- dimension 1: market regime
SELECT
    'by_regime'::TEXT                                                       AS dimension,
    COALESCE(ai_regime, 'unknown')::TEXT                                    AS bucket,
    COUNT(*)                                                                AS n_trades,
    COUNT(*) FILTER (WHERE net_pnl_usdt > 0)                               AS n_wins,
    ROUND(
        COUNT(*) FILTER (WHERE net_pnl_usdt > 0)::NUMERIC
        / NULLIF(COUNT(*), 0) * 100
    , 4)                                                                    AS win_rate_pct,
    SUM(net_pnl_usdt)                                                       AS net_pnl_usdt
FROM  master_trades
WHERE ai_prompt_version  = 'v3'
  AND exchange_order_id  IS NOT NULL
  AND ledger_audited_at  IS NOT NULL
GROUP BY ai_regime

UNION ALL

-- dimension 2: AI micro-gate path
SELECT
    'by_path'::TEXT                                                         AS dimension,
    COALESCE(ai_micro_gate_path, 'standard')::TEXT                         AS bucket,
    COUNT(*)                                                                AS n_trades,
    COUNT(*) FILTER (WHERE net_pnl_usdt > 0)                               AS n_wins,
    ROUND(
        COUNT(*) FILTER (WHERE net_pnl_usdt > 0)::NUMERIC
        / NULLIF(COUNT(*), 0) * 100
    , 4)                                                                    AS win_rate_pct,
    SUM(net_pnl_usdt)                                                       AS net_pnl_usdt
FROM  master_trades
WHERE ai_prompt_version  = 'v3'
  AND exchange_order_id  IS NOT NULL
  AND ledger_audited_at  IS NOT NULL
GROUP BY ai_micro_gate_path

UNION ALL

-- dimension 3: exit reason (take_profit | stop_loss | trailing_stop_tier_N | ...)
SELECT
    'by_exit_reason'::TEXT                                                  AS dimension,
    COALESCE(exit_reason, 'unknown')::TEXT                                  AS bucket,
    COUNT(*)                                                                AS n_trades,
    COUNT(*) FILTER (WHERE net_pnl_usdt > 0)                               AS n_wins,
    ROUND(
        COUNT(*) FILTER (WHERE net_pnl_usdt > 0)::NUMERIC
        / NULLIF(COUNT(*), 0) * 100
    , 4)                                                                    AS win_rate_pct,
    SUM(net_pnl_usdt)                                                       AS net_pnl_usdt
FROM  master_trades
WHERE ai_prompt_version  = 'v3'
  AND exchange_order_id  IS NOT NULL
  AND ledger_audited_at  IS NOT NULL
GROUP BY exit_reason

ORDER BY dimension, n_trades DESC;


COMMIT;
