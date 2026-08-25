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
    ? { ...derivStatus, last_decisions: (derivStatus.last_decisions || []).slice(-20) }
    : derivStatus;
  // derivClosedContracts grows without bound; 100 entries covers the visible trade history.
  const derivClosedTrimmed = (Array.isArray(derivClosedContracts) ? derivClosedContracts : []).slice(-100);

  // bot_state.json duplicates closed_trades/order_history/signal_history as large sub-arrays;
  // strip them here because they are already available at the top level with proper caps.
  const stateTrimmed = state
    ? { ...state, closed_trades: [], order_history: [], signal_history: [] }
    : state;

  const data = {
    state: stateTrimmed,
    status,
    control,
    orderHistory: (Array.isArray(orderHistory) ? orderHistory : []).slice(-25),
    signalHistory: (Array.isArray(signalHistory) ? signalHistory : []).slice(-25),
    scanHistory: (Array.isArray(scanHistory) ? scanHistory : []).slice(-5),
    openPositions,
    closedTrades: (Array.isArray(closedTrades) ? closedTrades : []).slice(-25),
    equityHistory: (Array.isArray(equityHistory) ? equityHistory : []).slice(-200),
    preFlight,
    tradeMonitorLog: (Array.isArray(tradeMonitorLog) ? tradeMonitorLog : []).slice(-50),
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