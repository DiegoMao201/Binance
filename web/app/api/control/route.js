import fs from "node:fs/promises";
import path from "node:path";

import { NextResponse } from "next/server";


const ROOT = path.join(process.cwd(), "..");
const LOGS_DIR = process.env.BOT_STATE_DIR || path.join(ROOT, "logs");
const CONTROL_PATH = path.join(LOGS_DIR, "control.json");


export const dynamic = "force-dynamic";


async function writeControlFile(payload) {
  await fs.mkdir(path.dirname(CONTROL_PATH), { recursive: true });
  await fs.writeFile(CONTROL_PATH, JSON.stringify(payload, null, 2), "utf8");
}


export async function POST(request) {
  const body = await request.json();

  // ── Acción especial: limpiar el flag de cierre fallido ──────────────────
  if (body?.action === "clear_close_error") {
    let current = {};
    try { current = JSON.parse(await fs.readFile(CONTROL_PATH, "utf8")); } catch {}
    const cleaned = { ...current };
    delete cleaned.manual_close_result;
    delete cleaned.manual_close_error;
    delete cleaned.manual_close_executed_at;
    await writeControlFile(cleaned);
    return NextResponse.json({ ok: true });
  }

  // ── Acción especial: resetear Kill Switch y reiniciar bot limpio ─────────
  if (body?.action === "reset_kill_switch") {
    const BOT_STATE   = path.join(LOGS_DIR, "bot_state.json");
    const EQ_HIST     = path.join(LOGS_DIR, "equity_history.json");
    const OPEN_POS    = path.join(LOGS_DIR, "open_positions.json");
    const RECOVERY    = path.join(LOGS_DIR, "recovery_status.json");

    // 1. Leer estado actual
    let botState = {};
    let equityHistory = [];
    try { botState       = JSON.parse(await fs.readFile(BOT_STATE, "utf8")); } catch {}
    try { equityHistory  = JSON.parse(await fs.readFile(EQ_HIST,   "utf8")); } catch {}

    // 2. Equity actual como nuevo punto de partida
    const currentEquity = Number(
      botState?.risk?.equity_usd  ||
      botState?.risk?.balance_usd ||
      (equityHistory.length ? equityHistory[equityHistory.length - 1]?.equity_usdt : 0) ||
      0
    );
    const resetEquity = currentEquity > 0 ? currentEquity : 0;

    // 3. Recalibrar equity_history: capear todos los HWM al nuevo reset point
    //    para que max(high_water_mark) == resetEquity → drawdown = 0 en próximo ciclo
    const now = new Date().toISOString();
    const recalibratedHistory = (Array.isArray(equityHistory) ? equityHistory : []).map(entry => ({
      ...entry,
      high_water_mark: Math.min(Number(entry.high_water_mark || 0), resetEquity > 0 ? resetEquity : Number(entry.high_water_mark || 0)),
    }));
    recalibratedHistory.push({ timestamp: now, equity_usdt: resetEquity, high_water_mark: resetEquity });

    // 4. Parchear bot_state.json: limpiar kill switch y resetear HWM
    const newBotState = {
      ...botState,
      risk: {
        ...(botState.risk || {}),
        kill_switch_triggered: false,
        drawdown_pct: 0,
        high_water_mark: resetEquity,
      },
    };

    // 5. control.json limpio → bot arranca en próximo ciclo
    const cleanControl = {
      desired_state: "running",
      updated_by: "dashboard",
      updated_at: now,
      reason: "Kill Switch reseteado manualmente. HWM recalibrado al equity actual.",
    };

    // 6. Escribir todos los archivos en paralelo
    await Promise.all([
      fs.writeFile(EQ_HIST,   JSON.stringify(recalibratedHistory, null, 2), "utf8"),
      fs.writeFile(BOT_STATE, JSON.stringify(newBotState,         null, 2), "utf8"),
      writeControlFile(cleanControl),
      fs.writeFile(OPEN_POS,  "[]",   "utf8"),
      fs.writeFile(RECOVERY,  "{}", "utf8"),
    ]);

    return NextResponse.json({ ok: true, reset_equity: resetEquity });
  }

  // ── Control normal: cambiar desired_state ───────────────────────────────
  const desiredState = body?.desiredState;
  const reason = body?.reason || "Cambio solicitado desde el dashboard.";

  if (!["running", "paused", "stopped"].includes(desiredState)) {
    return NextResponse.json({ error: "desiredState inválido" }, { status: 400 });
  }

  const payload = {
    desired_state: desiredState,
    reason,
    updated_by: "dashboard",
    updated_at: new Date().toISOString(),
  };

  await writeControlFile(payload);
  return NextResponse.json({ ok: true, control: payload });
}