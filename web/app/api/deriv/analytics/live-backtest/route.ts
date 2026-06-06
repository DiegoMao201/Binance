import { NextRequest, NextResponse } from 'next/server';
import { queryDerivAnalytics } from '../../../../../lib/deriv-db';

export async function GET(request: NextRequest) {
  try {
    const { searchParams } = new URL(request.url);
    const symbol = searchParams.get('symbol');
    const lookback = parseInt(searchParams.get('lookback') || '24');
    
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
    
    // Calculate live backtest metrics
    const query = `
      WITH recent_ticks AS (
        SELECT 
          captured_at,
          price,
          LAG(price) OVER (ORDER BY captured_at) as prev_price,
          CASE 
            WHEN price > LAG(price) OVER (ORDER BY captured_at) THEN 'UP'
            WHEN price < LAG(price) OVER (ORDER BY captured_at) THEN 'DOWN'
            ELSE 'SAME'
          END as direction,
          EXTRACT(EPOCH FROM (captured_at - LAG(captured_at) OVER (ORDER BY captured_at))) as time_diff_sec
        FROM deriv_tick_snapshots 
        WHERE symbol = $1 
          AND captured_at > NOW() - INTERVAL '${lookback} hours'
        ORDER BY captured_at DESC
        LIMIT 1000
      ),
      metrics AS (
        SELECT
          COUNT(*) as total_ticks,
          COUNT(*) FILTER (WHERE direction = 'UP') as up_ticks,
          COUNT(*) FILTER (WHERE direction = 'DOWN') as down_ticks,
          AVG(time_diff_sec) FILTER (WHERE time_diff_sec IS NOT NULL) as avg_time_diff_sec,
          STDDEV(time_diff_sec) FILTER (WHERE time_diff_sec IS NOT NULL) as std_time_diff_sec,
          MAX(price) as max_price,
          MIN(price) as min_price,
          MAX(price) - MIN(price) as price_range
        FROM recent_ticks
      )
      SELECT 
        total_ticks,
        up_ticks,
        down_ticks,
        ROUND(up_ticks::numeric / NULLIF(total_ticks, 0) * 100, 2) as up_percentage,
        ROUND(down_ticks::numeric / NULLIF(total_ticks, 0) * 100, 2) as down_percentage,
        ROUND(avg_time_diff_sec::numeric, 3) as avg_time_diff_sec,
        ROUND(std_time_diff_sec::numeric, 3) as std_time_diff_sec,
        ROUND(max_price::numeric, 2) as max_price,
        ROUND(min_price::numeric, 2) as min_price,
        ROUND(price_range::numeric, 2) as price_range
      FROM metrics
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
    console.error('Error calculating live backtest metrics:', error);
    return NextResponse.json(
      { error: 'Internal server error', details: error instanceof Error ? error.message : 'Unknown error' },
      { status: 500 }
    );
  }
}

export const dynamic = 'force-dynamic'; // Ensure this route is dynamic