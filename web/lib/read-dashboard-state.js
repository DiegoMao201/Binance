import fs from "node:fs/promises";
import path from "node:path";


const ROOT = path.join(process.cwd(), "..");
const LOGS = process.env.BOT_STATE_DIR || path.join(ROOT, "logs");
// Deriv corre como app separada en Coolify con su propio volumen → leer de allí
// si está montado, si no caer al mismo dir que Binance (modo dev local).
const DERIV_LOGS = process.env.DERIV_STATE_DIR || LOGS;


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

  return {
    state,
    status,
    control,
    orderHistory,
    signalHistory,
    scanHistory,
    openPositions,
    closedTrades,
    equityHistory,
    preFlight,
    tradeMonitorLog,
    recoveryStatus,
    derivStatus,
    derivOpenContracts,
    derivClosedContracts,
    // Phase 31: session management — null = show all-time stats
    derivSession,
    d10PanelState,
    serverTime: new Date().toISOString(),
  };
}