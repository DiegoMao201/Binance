import { Pool } from 'pg';

// Create a dedicated connection pool for Deriv analytics queries
const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  max: 5, // Maximum number of connections
  idleTimeoutMillis: 30000,
  connectionTimeoutMillis: 2000,
});

// Security: Only allow SELECT queries for analytics
export async function queryDerivAnalytics(sql: string, params: any[] = []) {
  const trimmedSql = sql.trim().toUpperCase();
  
  // Validate that it's a read-only query (SELECT or CTE starting with WITH)
  if (!trimmedSql.startsWith('SELECT') && !trimmedSql.startsWith('WITH')) {
    throw new Error('Only SELECT/WITH queries are allowed for analytics');
  }

  // Validate that it only queries deriv_* tables
  if (!trimmedSql.includes('DERIV_') && !trimmedSql.includes('V_DERIV_')) {
    throw new Error('Only deriv_* tables and views are allowed for analytics');
  }
  
  return pool.query(sql, params);
}

// Close the pool during application shutdown
export async function closeDerivPool() {
  await pool.end();
}

// Health check
export async function checkDerivDbHealth() {
  try {
    const result = await pool.query('SELECT 1 as health_check');
    return result.rows[0]?.health_check === 1;
  } catch {
    return false;
  }
}