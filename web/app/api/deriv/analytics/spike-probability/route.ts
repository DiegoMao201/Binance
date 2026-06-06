import { NextRequest, NextResponse } from 'next/server';
import { queryDerivAnalytics } from '../../../../../lib/deriv-db';

export async function GET(request: NextRequest) {
  try {
    const { searchParams } = new URL(request.url);
    const symbol = searchParams.get('symbol');
    const windowParam = searchParams.get('window') || '100,200,500';
    
    // Validate symbol
    const validSymbols = ['CRASH500', 'CRASH600', 'CRASH900'];
    if (!symbol || !validSymbols.includes(symbol)) {
      return NextResponse.json(
        { error: 'Invalid symbol. Must be one of: CRASH500, CRASH600, CRASH900' },
        { status: 400 }
      );
    }
    
    // Parse window parameters
    const windows = windowParam.split(',').map(w => parseInt(w)).filter(w => !isNaN(w) && w > 0);
    if (windows.length === 0) {
      return NextResponse.json(
        { error: 'Invalid window parameter. Must be comma-separated positive integers' },
        { status: 400 }
      );
    }
    
    // Calculate spike probability for each window
    const probabilities: Record<string, number> = {};
    
    for (const windowSize of windows) {
      const query = `
        SELECT 
          SUM(CASE WHEN score > 7.0 THEN 1 ELSE 0 END) as spike_count,
          COUNT(*) as total_count
        FROM (
          SELECT score
          FROM deriv_tick_snapshots 
          WHERE symbol = $1 
            AND captured_at > NOW() - INTERVAL '24 hours'
          ORDER BY captured_at DESC
          LIMIT $2
        ) as subquery
      `;
      
      const result = await queryDerivAnalytics(query, [symbol, windowSize]);
      
      if (result.rows.length > 0) {
        const { spike_count, total_count } = result.rows[0];
        const probability = total_count > 0 ? spike_count / total_count : 0;
        probabilities[windowSize] = Math.round(probability * 100) / 100; // Round to 2 decimal places
      } else {
        probabilities[windowSize] = 0;
      }
    }
    
    return NextResponse.json({
      symbol,
      probabilities,
      timestamp: new Date().toISOString()
    });
    
  } catch (error) {
    console.error('Error calculating spike probability:', error);
    return NextResponse.json(
      { error: 'Internal server error', details: error instanceof Error ? error.message : 'Unknown error' },
      { status: 500 }
    );
  }
}

export const dynamic = 'force-dynamic'; // Ensure this route is dynamic