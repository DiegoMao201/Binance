import fs from "node:fs/promises";
import path from "node:path";


const ROOT = path.join(process.cwd(), "..");
const LOGS = process.env.BOT_STATE_DIR || path.join(ROOT, "logs");
// Deriv corre como app separada en Coolify con su propio volumen → leer de allí
// si está montado, si no caer al mismo dir que Binance (modo dev local).
const DERIV_LOGS = process.env.DERIV_STATE_DIR || LOGS;

// Per-worker in-memory cache: avoids reading 17 JSON files on every request.
const _CACHE_TTL = 20_000; // 20 s
let _cache = null;


async function readJson(fileName, fallback, baseDir = LOGS) {
  try {
    const fullPath = path.join(baseDir, fileName);
    const content = await fs.readFile(fullPath, "utf8");
    return JSON.parse(content);
  } catch (error) {
    console.error(`[dashboard-state] fallback for ${fileName}:`, error?.message || error);
    return fallback;
  }
}


export async function readDashboardState() {
  const now = Date.now();
  if (_cache && now - _cache.ts < _CACHE_TTL) return _cache.data;

  const [state, status, control, orderHistory, signalHistory, scanHistory, openPositions, closedTrades, equityHistory, preFlight, tradeMonitorLog, recoveryStatus, derivStatus, derivOpenContracts, derivClosedContracts, derivSession, d10PanelState] = await Promise.all([
    readJson("bot_state.json", {}),
    readJson("status.json", {}),
    readJson("control.json", {}),
    readJson("order_history.json", []),
    readJson("signal_history.json", []),
    readJson("scan_history.json", []),
    readJson("open_positions.json", []),
    readJson("closed_trades.json", []),
    readJson("equity_history.json", []),
    readJson("pre_flight.json", {}),
    readJson("trade_monitor_log.json", []),
    readJson("recovery_status.json", {}),
    readJson("deriv_status.json", {}, DERIV_LOGS),
    readJson("deriv_open_contracts.json", [], DERIV_LOGS),
    readJson("deriv_closed_contracts.json", [], DERIV_LOGS),
    readJson("deriv_session.json", null, DERIV_LOGS),
    readJson("d10_panel_state.json", null, DERIV_LOGS),
  ]);

  // Truncate large arrays before sending to the client.
  // last_decisions can grow to hundreds of entries; 50 is enough for the feed display.
  const derivStatusTrimmed = derivStatus
    ? { ...derivStatus, last_decisions: (derivStatus.last_decisions || []).slice(-50) }
    : derivStatus;
  // derivClosedContracts grows without bound; 200 entries covers the visible trade history.
  const derivClosedTrimmed = (Array.isArray(derivClosedContracts) ? derivClosedContracts : []).slice(-200);

  // bot_state.json duplicates closed_trades/order_history/signal_history as sub-arrays;
  // cap them here so the same data isn't sent twice at full size.
  const stateTrimmed = state
    ? {
        ...state,
        closed_trades: (state.closed_trades || []).slice(-100),
        order_history: (state.order_history || []).slice(-100),
        signal_history: (state.signal_history || []).slice(-100),
      }
    : state;

  const data = {
    state: stateTrimmed,
    status,
    control,
    orderHistory: (Array.isArray(orderHistory) ? orderHistory : []).slice(-100),
    signalHistory: (Array.isArray(signalHistory) ? signalHistory : []).slice(-100),
    scanHistory: (Array.isArray(scanHistory) ? scanHistory : []).slice(-20),
    openPositions,
    closedTrades: (Array.isArray(closedTrades) ? closedTrades : []).slice(-100),
    equityHistory,
    preFlight,
    tradeMonitorLog: (Array.isArray(tradeMonitorLog) ? tradeMonitorLog : []).slice(-100),
    recoveryStatus,
    derivStatus: derivStatusTrimmed,
    derivOpenContracts,
    derivClosedContracts: derivClosedTrimmed,
    // Phase 31: session management — null = show all-time stats
    derivSession,
    d10PanelState,
    serverTime: new Date().toISOString(),
  };

  _cache = { ts: now, data };
  return data;
}