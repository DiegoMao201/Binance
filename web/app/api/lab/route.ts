import { NextResponse } from "next/server";
import { queryBinance, checkBinanceHealth } from "@/lib/binance-db";

export const dynamic = "force-dynamic";

const LAB_STRATEGIES = [
  "baseline_random",        // ctrl_random_maker (existing)
  "ctrl_random_taker",
  "rsi_reversion_maker",   "rsi_reversion_taker",
  "ema_cross_maker",        "ema_cross_taker",
  "bollinger_reversion_maker", "bollinger_reversion_taker",
  "volume_breakout_maker",  "volume_breakout_taker",
  "book_imbalance_maker",   "book_imbalance_taker",
  "taker_flow_maker",       "taker_flow_taker",
  "oi_delta_maker",         "oi_delta_taker",
];

const CTRL_MAKER = "baseline_random";
const CTRL_TAKER = "ctrl_random_taker";

function isTakerStrategy(name: string): boolean {
  return name.endsWith("_taker") || name === "ctrl_random_taker";
}

function getMode(name: string): string {
  if (name === "baseline_random") return "MAKER";
  if (name.endsWith("_taker")) return "TAKER";
  if (name.endsWith("_maker")) return "MAKER";
  return "REAL";
}

function getDisplayName(name: string): string {
  return name === "baseline_random" ? "ctrl_random_maker" : name;
}

function isControl(name: string): boolean {
  return name === "baseline_random" || name === "ctrl_random_taker";
}

// Normal-approximation CI95 lower bound
function ci95inf(mean: number | null, std: number | null, n: number): number | null {
  if (mean === null || std === null || n < 2) return null;
  return mean - 1.96 * std / Math.sqrt(n);
}

function determineStatus(
  name: string,
  n_ef: number,
  neto_bps: number | null,
  ci_inf: number | null,
  ctrl_bps: number | null
): string {
  if (isControl(name)) return "CONTROL";
  if (n_ef < 30) return "INSUFICIENTE";
  if (
    neto_bps !== null &&
    ctrl_bps !== null &&
    neto_bps > ctrl_bps + 10 &&
    ci_inf !== null && ci_inf > 0
  ) return "CANDIDATA";
  return "DESCARTADA";
}

export async function GET() {
  const placeholders = LAB_STRATEGIES.map((_, i) => `$${i + 1}`).join(",");

  const [dbOk, summaryRes, hoursRes, histRes, healthRes] = await Promise.allSettled([
    checkBinanceHealth(),
    queryBinance(`
      SELECT
        strategy_name,
        COUNT(*) AS signals,
        COUNT(*) FILTER (WHERE status = 'FILLED') AS fills,
        COUNT(DISTINCT fill_event_id) FILTER (WHERE status = 'FILLED') AS n_ef,
        MIN(signal_ts) AS first_signal,
        MAX(fill_ts)   AS last_fill,
        AVG(markout_1s_bps)  FILTER (WHERE status = 'FILLED') AS avg_1s,
        STDDEV(markout_1s_bps) FILTER (WHERE status = 'FILLED') AS std_1s,
        AVG(markout_5s_bps)  FILTER (WHERE status = 'FILLED') AS avg_5s,
        AVG(markout_30s_bps) FILTER (WHERE status = 'FILLED') AS avg_30s,
        AVG(markout_60s_bps) FILTER (WHERE status = 'FILLED') AS avg_60s,
        STDDEV(markout_60s_bps) FILTER (WHERE status = 'FILLED') AS std_60s,
        AVG(markout_5m_bps)  FILTER (WHERE status = 'FILLED') AS avg_5m,
        AVG(markout_15m_bps) FILTER (WHERE status = 'FILLED') AS avg_15m,
        AVG(markout_30m_bps) FILTER (WHERE status = 'FILLED') AS avg_30m,
        AVG(markout_60m_bps) FILTER (WHERE status = 'FILLED') AS avg_60m
      FROM shadow_trades
      WHERE signal_ts > NOW() - INTERVAL '7 days'
        AND strategy_name IN (${placeholders})
      GROUP BY strategy_name
    `, LAB_STRATEGIES),
    queryBinance(`
      SELECT
        strategy_name,
        EXTRACT(HOUR FROM fill_ts AT TIME ZONE 'UTC')::int AS utc_hour,
        COUNT(*) AS cnt
      FROM shadow_trades
      WHERE status = 'FILLED'
        AND fill_ts > NOW() - INTERVAL '7 days'
        AND strategy_name IN (${placeholders})
      GROUP BY strategy_name, utc_hour
    `, LAB_STRATEGIES),
    queryBinance(`
      SELECT strategy_name, markout_60s_bps
      FROM shadow_trades
      WHERE status = 'FILLED'
        AND markout_60s_bps IS NOT NULL
        AND fill_ts > NOW() - INTERVAL '7 days'
        AND strategy_name IN (${placeholders})
      ORDER BY RANDOM()
      LIMIT 3000
    `, LAB_STRATEGIES),
    queryBinance(`
      SELECT stream_name, status, gap_count, updated_at,
             EXTRACT(EPOCH FROM (NOW() - updated_at))::int AS seconds_ago
      FROM recorder_health
      ORDER BY stream_name
    `),
  ]);

  const summary = summaryRes.status === "fulfilled" ? summaryRes.value.rows : [];
  const hours   = hoursRes.status === "fulfilled"   ? hoursRes.value.rows   : [];
  const hist    = histRes.status === "fulfilled"    ? histRes.value.rows    : [];
  const health  = healthRes.status === "fulfilled"  ? healthRes.value.rows  : [];
  const db_ok   = dbOk.status === "fulfilled"       ? dbOk.value            : false;

  // Index by strategy_name
  const byName = Object.fromEntries(summary.map((r: Record<string, unknown>) => [r.strategy_name as string, r]));

  // Control baselines
  const ctrlMakerRow = byName[CTRL_MAKER];
  const ctrlTakerRow = byName[CTRL_TAKER];
  const ctrl_maker_bps = ctrlMakerRow ? Number(ctrlMakerRow.avg_60s ?? 0) : null;
  const ctrl_taker_bps = ctrlTakerRow ? Number(ctrlTakerRow.avg_60s ?? 0) : null;

  // UTC hour distribution per strategy
  const hoursByName: Record<string, number[]> = {};
  for (const row of hours as Record<string, unknown>[]) {
    const name = row.strategy_name as string;
    if (!hoursByName[name]) hoursByName[name] = new Array(24).fill(0);
    hoursByName[name][Number(row.utc_hour)] = Number(row.cnt);
  }

  // Markout hist sample per strategy
  const histByName: Record<string, number[]> = {};
  for (const row of hist as Record<string, unknown>[]) {
    const name = row.strategy_name as string;
    if (!histByName[name]) histByName[name] = [];
    histByName[name].push(Number(row.markout_60s_bps));
  }

  // Build strategy list (all 16, even if no data yet)
  const strategies = LAB_STRATEGIES.map((name) => {
    const row = byName[name];
    const fills = row ? Number(row.fills) : 0;
    const n_ef  = row ? Number(row.n_ef)  : 0;
    const neto  = row ? (row.avg_60s != null ? Number(row.avg_60s) : null) : null;
    const std   = row ? (row.std_60s != null ? Number(row.std_60s) : null) : null;
    const ci_inf = ci95inf(neto, std, n_ef);
    const ctrl_bps = isTakerStrategy(name) ? ctrl_taker_bps : ctrl_maker_bps;

    return {
      name,
      display_name: getDisplayName(name),
      mode: getMode(name),
      signals: row ? Number(row.signals) : 0,
      fills,
      n_ef,
      neto_bps: neto,
      ci95_inf: ci_inf,
      vs_ctrl: neto !== null && ctrl_bps !== null ? neto - ctrl_bps : null,
      status: determineStatus(name, n_ef, neto, ci_inf, ctrl_bps),
      first_signal: row?.first_signal ?? null,
      last_fill: row?.last_fill ?? null,
      markout_curve: {
        "1s":  row?.avg_1s  != null ? Number(row.avg_1s)  : null,
        "5s":  row?.avg_5s  != null ? Number(row.avg_5s)  : null,
        "30s": row?.avg_30s != null ? Number(row.avg_30s) : null,
        "60s": row?.avg_60s != null ? Number(row.avg_60s) : null,
        "5m":  row?.avg_5m  != null ? Number(row.avg_5m)  : null,
        "15m": row?.avg_15m != null ? Number(row.avg_15m) : null,
        "30m": row?.avg_30m != null ? Number(row.avg_30m) : null,
        "60m": row?.avg_60m != null ? Number(row.avg_60m) : null,
      },
      utc_hours: hoursByName[name] ?? new Array(24).fill(0),
      markout_hist: histByName[name] ?? [],
    };
  });

  // Lab start time: min first_signal across lab strategies (excluding controls for cleanliness)
  const labStrategies = strategies.filter((s) => !isControl(s.name) && s.first_signal);
  const lab_started_at = labStrategies.length > 0
    ? labStrategies.reduce((min, s) =>
        s.first_signal! < min ? s.first_signal! : min,
        labStrategies[0].first_signal!)
    : null;

  const total_fills_24h = summary.reduce((acc: number, r: Record<string, unknown>) =>
    acc + Number(r.fills ?? 0), 0);

  const last_fill_ts = summary.reduce((max: string | null, r: Record<string, unknown>) => {
    if (!r.last_fill) return max;
    if (!max || r.last_fill > max) return r.last_fill as string;
    return max;
  }, null);

  return NextResponse.json({
    generated_at: new Date().toISOString(),
    lab_started_at,
    strategies,
    system: {
      db_ok,
      streams: health.map((h: Record<string, unknown>) => ({
        name: h.stream_name,
        status: h.status,
        gap_count: Number(h.gap_count ?? 0),
        seconds_ago: Number(h.seconds_ago ?? 9999),
      })),
      total_fills_24h,
      last_fill_ts,
    },
  }, { headers: { "Cache-Control": "no-store, max-age=0" } });
}
