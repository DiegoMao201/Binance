-- Migration 007: Make user_trade_allocations.master_trade_id nullable
--
-- Root cause: Prisma schema never included master_trade_id (it was added only
-- in the raw SQL schema). When Prisma inserts allocation rows it omits the
-- column, triggering a NOT NULL constraint violation → webhook HTTP 500.
--
-- Fix: drop the NOT NULL constraint. The FK reference is preserved so existing
-- data with a master_trade_id remains valid and queryable.

ALTER TABLE user_trade_allocations
  ALTER COLUMN master_trade_id DROP NOT NULL;
