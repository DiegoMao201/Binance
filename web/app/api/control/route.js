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

  // Acción especial: limpiar el flag de cierre fallido
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