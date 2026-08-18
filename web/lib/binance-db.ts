import { Pool } from "pg";

// Separate connection pool for the Binance recorder DB (optiferre_binance).
// The main app's DATABASE_URL points to the OptiFerre DB used by Prisma.
// BINANCE_DB_URL must be set in the frontend container's env vars.
const pool = new Pool({
  connectionString: process.env.BINANCE_DB_URL,
  max: 5,
  idleTimeoutMillis: 30_000,
  connectionTimeoutMillis: 3_000,
});

export async function queryBinance(sql: string, params: unknown[] = []) {
  return pool.query(sql, params);
}

export async function checkBinanceHealth(): Promise<boolean> {
  try {
    await pool.query("SELECT 1");
    return true;
  } catch {
    return false;
  }
}
