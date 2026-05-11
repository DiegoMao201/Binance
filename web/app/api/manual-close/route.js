import fs from "node:fs/promises";
import path from "node:path";

import { NextResponse } from "next/server";


const ROOT = path.join(process.cwd(), "..");
const LOGS_DIR = process.env.BOT_STATE_DIR || path.join(ROOT, "logs");
const CONTROL_PATH = path.join(LOGS_DIR, "control.json");


export const dynamic = "force-dynamic";


async function readControlFile() {
  try {
    const content = await fs.readFile(CONTROL_PATH, "utf8");
    return JSON.parse(content);
  } catch {
    return { desired_state: "running" };
  }
}

async function writeControlFile(payload) {
  await fs.mkdir(path.dirname(CONTROL_PATH), { recursive: true });
  await fs.writeFile(CONTROL_PATH, JSON.stringify(payload, null, 2), "utf8");
}


/**
 * POST /api/manual-close
 * Body: { symbol: "BNB/USDT" }
 *
 * Escribe un command flag en control.json que el main_loop leerá
 * en el próximo ciclo (≤60s) y ejecutará el cierre vía executor.
 * NO llama a Binance directamente: todo pasa por el bot.
 */
export async function POST(request) {
  const body = await request.json();
  const symbol = body?.symbol;

  if (!symbol || typeof symbol !== "string") {
    return NextResponse.json({ error: "symbol requerido" }, { status: 400 });
  }

  const current = await readControlFile();

  const payload = {
    ...current,
    manual_close_request: true,
    manual_close_symbol: symbol.trim().toUpperCase(),
    manual_close_requested_at: new Date().toISOString(),
    manual_close_requested_by: "dashboard",
  };

  await writeControlFile(payload);
  return NextResponse.json({ ok: true, symbol: payload.manual_close_symbol });
}
