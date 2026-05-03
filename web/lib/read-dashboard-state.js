import fs from "node:fs/promises";
import path from "node:path";


const ROOT = path.join(process.cwd(), "..");
const LOGS = process.env.BOT_STATE_DIR || path.join(ROOT, "logs");


async function readJson(fileName, fallback) {
  try {
    const fullPath = path.join(LOGS, fileName);
    const content = await fs.readFile(fullPath, "utf8");
    return JSON.parse(content);
  } catch {
    return fallback;
  }
}


export async function readDashboardState() {
  const [state, status, control, orderHistory, signalHistory, openPositions, closedTrades] = await Promise.all([
    readJson("bot_state.json", {}),
    readJson("status.json", {}),
    readJson("control.json", {}),
    readJson("order_history.json", []),
    readJson("signal_history.json", []),
    readJson("open_positions.json", []),
    readJson("closed_trades.json", []),
  ]);

  return {
    state,
    status,
    control,
    orderHistory,
    signalHistory,
    openPositions,
    closedTrades,
    serverTime: new Date().toISOString(),
  };
}