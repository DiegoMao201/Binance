import fs from "node:fs/promises";
import path from "node:path";


const ROOT = path.join(process.cwd(), "..");
const LOGS = process.env.BOT_STATE_DIR || path.join(ROOT, "logs");


async function readJson(fileName, fallback) {
  try {
    const fullPath = path.join(LOGS, fileName);
    const content = await fs.readFile(fullPath, "utf8");
    return JSON.parse(content);
  } catch (error) {
    console.error(`[dashboard-state] fallback for ${fileName}:`, error?.message || error);
    return fallback;
  }
}


export async function readDashboardState() {
  const [state, status, control, orderHistory, signalHistory, scanHistory, openPositions, closedTrades, equityHistory, preFlight, tradeMonitorLog, recoveryStatus, derivStatus, derivOpenContracts, derivClosedContracts] = await Promise.all([
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
    readJson("deriv_status.json", {}),
    readJson("deriv_open_contracts.json", []),
    readJson("deriv_closed_contracts.json", []),
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
    serverTime: new Date().toISOString(),
  };
}