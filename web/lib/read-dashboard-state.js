import fs from "node:fs/promises";
import path from "node:path";


const ROOT = path.join(process.cwd(), "..");
const LOGS = path.join(ROOT, "logs");


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
  const [state, status, control, orderHistory, signalHistory] = await Promise.all([
    readJson("bot_state.json", {}),
    readJson("status.json", {}),
    readJson("control.json", {}),
    readJson("order_history.json", []),
    readJson("signal_history.json", []),
  ]);

  return {
    state,
    status,
    control,
    orderHistory,
    signalHistory,
    serverTime: new Date().toISOString(),
  };
}