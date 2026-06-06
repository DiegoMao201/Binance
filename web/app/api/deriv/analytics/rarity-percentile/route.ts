import { NextRequest, NextResponse } from 'next/server';
import { queryDerivAnalytics } from '../../../../../lib/deriv-db';

export async function GET(request: NextRequest) {
  try {
    const { searchParams } = new URL(request.url);
    const symbol = searchParams.get('symbol');
    const lookback = parseInt(searchParams.get('lookback') || '72');
    
    // Validate symbol
    const validSymbols = ['CRASH500', 'CRASH600', 'CRASH900'];
    if (!symbol || !validSymbols.includes(symbol)) {
      return NextResponse.json(
        { error: 'Invalid symbol. Must be one of: CRASH500, CRASH600, CRASH900' },
        { status: 400 }
      );
    }
    
    // Validate lookback (hours)
    if (isNaN(lookback) || lookback < 1 || lookback > 168) {
      return NextResponse.json(
        { error: 'Invalid lookback. Must be between 1 and 168 hours' },
        { status: 400 }
      );
    }
    
    // Calculate rarity percentile metrics
    const query = `
      WITH tick_stats AS (
        SELECT 
          price,
          LAG(price) OVER (ORDER BY captured_at) as prev_price,
          CASE 
            WHEN price > LAG(price) OVER (ORDER BY captured_at) THEN price - LAG(price) OVER (ORDER BY captured_at)
            WHEN price < LAG(price) OVER (ORDER BY captured_at) THEN LAG(price) OVER (ORDER BY captured_at) - price
            ELSE 0
          END as price_change,
          EXTRACT(EPOCH FROM (captured_at - LAG(captured_at) OVER (ORDER BY captured_at))) as time_diff_sec
        FROM deriv_tick_snapshots 
        WHERE symbol = $1 
          AND captured_at > NOW() - INTERVAL '${lookback} hours'
        ORDER BY captured_at DESC
        LIMIT 2000
      ),
      current_tick AS (
        SELECT 
          price_change as current_change,
          time_diff_sec as current_time_diff
        FROM tick_stats
        ORDER BY (SELECT 1) -- Get the most recent tick
        LIMIT 1
      ),
      distribution AS (
        SELECT 
          price_change,
          time_diff_sec,
          PERCENT_RANK() OVER (ORDER BY price_change) as price_change_percentile,
          PERCENT_RANK() OVER (ORDER BY time_diff_sec) as time_diff_percentile
        FROM tick_stats
        WHERE price_change IS NOT NULL 
          AND time_diff_sec IS NOT NULL
      ),
      current_percentiles AS (
        SELECT 
          d.price_change_percentile,
          d.time_diff_percentile,
          CASE 
            WHEN d.price_change_percentile >= 0.9 THEN 'EXTREME_RARE'
            WHEN d.price_change_percentile >= 0.75 THEN 'RARE'
            WHEN d.price_change_percentile >= 0.5 THEN 'COMMON'
            ELSE 'VERY_COMMON'
          END as rarity_category
        FROM distribution d
        CROSS JOIN current_tick ct
        WHERE d.price_change = ct.current_change
          AND d.time_diff_sec = ct.current_time_diff
        LIMIT 1
      ),
      summary_stats AS (
        SELECT 
          COUNT(*) as total_ticks,
          AVG(price_change) as avg_price_change,
          STDDEV(price_change) as std_price_change,
          MAX(price_change) as max_price_change,
          MIN(price_change) as min_price_change,
          PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY price_change) as p90_price_change,
          PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY price_change) as p75_price_change,
          PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price_change) as p50_price_change
        FROM tick_stats
        WHERE price_change IS NOT NULL
      )
      SELECT 
        cp.price_change_percentile,
        cp.time_diff_percentile,
        cp.rarity_category,
        ss.total_ticks,
        ROUND(ss.avg_price_change::numeric, 4) as avg_price_change,
        ROUND(ss.std_price_change::numeric, 4) as std_price_change,
        ROUND(ss.max_price_change::numeric, 4) as max_price_change,
        ROUND(ss.min_price_change::numeric, 4) as min_price_change,
        ROUND(ss.p90_price_change::numeric, 4) as p90_price_change,
        ROUND(ss.p75_price_change::numeric, 4) as p75_price_change,
        ROUND(ss.p50_price_change::numeric, 4) as p50_price_change
      FROM current_percentiles cp
      CROSS JOIN summary_stats ss
    `;
    
    const result = await queryDerivAnalytics(query, [symbol]);
    
    if (result.rows.length > 0) {
      const metrics = result.rows[0];
      
      return NextResponse.json({
        symbol,
        lookback_hours: lookback,
        metrics,
        timestamp: new Date().toISOString()
      });
    } else {
      return NextResponse.json({
        symbol,
        lookback_hours: lookback,
        metrics: null,
        error: 'No data available',
        timestamp: new Date().toISOString()
      });
    }
    
  } catch (error) {
    console.error('Error calculating rarity percentile:', error);
    return NextResponse.json(
      { error: 'Internal server error', details: error instanceof Error ? error.message : 'Unknown error' },
      { status: 500 }
    );
  }
}

export const dynamic = 'force-dynamic'; // Ensure this route is dynamic