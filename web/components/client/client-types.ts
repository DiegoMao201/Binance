/**
 * components/client/client-types.ts
 *
 * Shared serialisable types passed from Server Components to Client Components.
 * ALL monetary fields are `string` (converted from Prisma.Decimal on the server).
 * ALL date fields are ISO 8601 strings (converted from Date on the server).
 *
 * This module has NO runtime logic — pure type definitions.
 */

/** One row in the client's trade history table. */
export type TradeRow = {
  /** alloc_id from user_trade_allocations (BigInt → string). */
  id: string;
  /** E.g. "BTCUSDT" */
  symbol: string;
  /** "buy" | "sell" */
  side: string;
  /** master_trades.opened_at — ISO 8601 string */
  openedAt: string;
  /** master_trades.closed_at — ISO 8601 string */
  closedAt: string;
  /** user_trade_allocations.allocated_at — ISO 8601 string */
  allocatedAt: string;
  /** gross_user_pnl_usdt — toFixed(2) string. Positive or negative. */
  grossPnl: string;
  /** admin_fee_usdt — toFixed(2) string. Always >= 0. */
  adminFee: string;
  /** user_net_pnl_usdt — toFixed(2) string. Post-fee. */
  netPnl: string;
};

/** One data point for the equity curve chart. */
export type EquityPoint = {
  /** Date in "YYYY-MM-DD" format for the X axis. */
  date: string;
  /** Running balance — toFixed(2) string. */
  balance: string;
};
