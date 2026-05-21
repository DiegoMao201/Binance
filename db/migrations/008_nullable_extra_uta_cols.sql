-- Migration 008: Drop NOT NULL on extra columns in user_trade_allocations
-- that exist in the DB (from legacy Python bot) but are absent from the
-- Prisma schema. Without this, any Prisma INSERT from the webhook fails
-- because the DB rejects NULL values for these columns.
--
-- Columns fixed:
--   allocation_share_pct       (already applied in 007, idempotent)
--   user_capital_at_risk_usdt  (Python PAMM legacy field)
--   gross_user_pnl_usdt        (Python PAMM legacy field)
--   performance_fee_pct_applied(Python PAMM legacy field)
--   user_net_pnl_usdt          (Python PAMM legacy field)

ALTER TABLE user_trade_allocations
    ALTER COLUMN allocation_share_pct        DROP NOT NULL,
    ALTER COLUMN user_capital_at_risk_usdt   DROP NOT NULL,
    ALTER COLUMN gross_user_pnl_usdt         DROP NOT NULL,
    ALTER COLUMN performance_fee_pct_applied DROP NOT NULL,
    ALTER COLUMN user_net_pnl_usdt           DROP NOT NULL;
