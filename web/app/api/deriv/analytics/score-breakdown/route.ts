import { NextRequest, NextResponse } from 'next/server';
import { queryDerivAnalytics } from '../../../../../lib/deriv-db';

export const runtime = 'nodejs';

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
    
    // Get the latest tick snapshot with score breakdown
    const query = `
      SELECT 
        score,
        regime,
        score_breakdown,
        captured_at
      FROM deriv_tick_snapshots 
      WHERE symbol = $1 
        AND score_breakdown IS NOT NULL
      ORDER BY captured_at DESC 
      LIMIT 1
    `;
    
    const result = await queryDerivAnalytics(query, [symbol]);
    
    if (result.rows.length === 0) {
      return NextResponse.json({
        symbol,
        total_score: null,
        components: null,
        timestamp: new Date().toISOString(),
        message: 'No score breakdown data available'
      });
    }
    
    const row = result.rows[0];
    const breakdown = row.score_breakdown || {};
    
    // Parse score breakdown components
    const components = {
      hurst: breakdown.hurst ? parseFloat(breakdown.hurst) : 0,
      autocorr: breakdown.autocorr ? parseFloat(breakdown.autocorr) : 0,
      volatility: breakdown.volatility ? parseFloat(breakdown.volatility) : 0,
      regime: breakdown.regime ? parseFloat(breakdown.regime) : 0,
      ai_confidence: breakdown.ai_confidence ? parseFloat(breakdown.ai_confidence) : 0,
      trend_strength: breakdown.trend_strength ? parseFloat(breakdown.trend_strength) : 0,
      atr_percentile: breakdown.atr_percentile ? parseFloat(breakdown.atr_percentile) : 0,
      market_phase: breakdown.market_phase ? parseFloat(breakdown.market_phase) : 0,
    };
    
    // Calculate total from components if needed
    const totalFromComponents = Object.values(components).reduce((sum, val) => sum + val, 0);
    const totalScore = row.score ? parseFloat(row.score) : totalFromComponents;
    
    return NextResponse.json({
      symbol,
      total_score: totalScore,
      regime: row.regime,
      components,
      last_updated: row.captured_at,
      timestamp: new Date().toISOString()
    });
    
  } catch (error) {
    console.error('Error fetching score breakdown:', error);
    return NextResponse.json(
      { error: 'Internal server error', details: error instanceof Error ? error.message : 'Unknown error' },
      { status: 500 }
    );
  }
}

export const dynamic = 'force-dynamic';