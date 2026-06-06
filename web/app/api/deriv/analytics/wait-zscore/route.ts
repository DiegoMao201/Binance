import { NextRequest, NextResponse } from 'next/server';
import { queryDerivAnalytics } from '../../../../../lib/deriv-db';

export async function GET(request: NextRequest) {
  try {
    const { searchParams } = new URL(request.url);
    const symbol = searchParams.get('symbol');
    
    // Validate symbol
    const validSymbols = ['CRASH500', 'CRASH600', 'CRASH900'];
    if (!symbol || !validSymbols.includes(symbol)) {
      return NextResponse.json(
        { error: 'Invalid symbol. Must be one of: CRASH500, CRASH600, CRASH900' },
        { status: 400 }
      );
    }
    
    // Get the time since last spike (score > 7.0)
    const lastSpikeQuery = `
      SELECT 
        EXTRACT(EPOCH FROM (NOW() - captured_at)) as seconds_since_spike
      FROM deriv_tick_snapshots 
      WHERE symbol = $1 
        AND score > 7.0
      ORDER BY captured_at DESC 
      LIMIT 1
    `;
    
    const lastSpikeResult = await queryDerivAnalytics(lastSpikeQuery, [symbol]);
    
    if (lastSpikeResult.rows.length === 0) {
      return NextResponse.json({
        symbol,
        current_wait_seconds: null,
        z_score: null,
        classification: 'NO_HISTORY',
        timestamp: new Date().toISOString(),
        message: 'No spike history available'
      });
    }
    
    const currentWaitSeconds = parseFloat(lastSpikeResult.rows[0].seconds_since_spike);
    
    // Get historical wait time distribution (last 7 days)
    const distributionQuery = `
      WITH spike_times AS (
        SELECT 
          captured_at,
          LAG(captured_at) OVER (ORDER BY captured_at) as prev_captured_at
        FROM deriv_tick_snapshots 
        WHERE symbol = $1 
          AND score > 7.0
          AND captured_at > NOW() - INTERVAL '7 days'
      )
      SELECT 
        EXTRACT(EPOCH FROM (captured_at - prev_captured_at)) as wait_seconds
      FROM spike_times
      WHERE prev_captured_at IS NOT NULL
        AND EXTRACT(EPOCH FROM (captured_at - prev_captured_at)) > 0
    `;
    
    const distributionResult = await queryDerivAnalytics(distributionQuery, [symbol]);
    
    if (distributionResult.rows.length < 10) {
      return NextResponse.json({
        symbol,
        current_wait_seconds: currentWaitSeconds,
        z_score: null,
        classification: 'INSUFFICIENT_DATA',
        timestamp: new Date().toISOString(),
        message: 'Insufficient historical data for Z-score calculation'
      });
    }
    
    // Calculate mean and standard deviation
    const waitTimes = distributionResult.rows.map(row => parseFloat(row.wait_seconds));
    const mean = waitTimes.reduce((sum, val) => sum + val, 0) / waitTimes.length;
    const variance = waitTimes.reduce((sum, val) => sum + Math.pow(val - mean, 2), 0) / waitTimes.length;
    const stdDev = Math.sqrt(variance);
    
    // Calculate Z-score
    const zScore = stdDev > 0 ? (currentWaitSeconds - mean) / stdDev : 0;
    
    // Classify based on Z-score
    let classification = 'NORMAL';
    if (zScore > 2.0) classification = 'EXTREMO';
    else if (zScore > 1.0) classification = 'EXTENDIDO';
    else if (zScore < -2.0) classification = 'RÁPIDO';
    else if (zScore < -1.0) classification = 'ACELERADO';
    
    return NextResponse.json({
      symbol,
      current_wait_seconds: Math.round(currentWaitSeconds),
      z_score: Math.round(zScore * 100) / 100,
      classification,
      distribution: {
        mean: Math.round(mean),
        std_dev: Math.round(stdDev),
        sample_size: waitTimes.length
      },
      timestamp: new Date().toISOString()
    });
    
  } catch (error) {
    console.error('Error calculating wait Z-score:', error);
    return NextResponse.json(
      { error: 'Internal server error', details: error instanceof Error ? error.message : 'Unknown error' },
      { status: 500 }
    );
  }
}

export const dynamic = 'force-dynamic';