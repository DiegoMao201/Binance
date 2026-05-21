/**
 * /api/session — Phase 31 session management
 *
 * GET  → returns current session start timestamp (from logs/deriv_session.json)
 * POST → resets the session start to now (writes logs/deriv_session.json)
 *
 * This is a VISUAL-ONLY reset: the bot keeps running without interruption.
 * All KPI stats (PnL, trades, W/L) in the HUD are filtered from this timestamp.
 */
import fs from "node:fs/promises";
import path from "node:path";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const ROOT = path.join(process.cwd(), "..");
const LOGS_DIR = process.env.BOT_STATE_DIR || path.join(ROOT, "logs");
const SESSION_PATH = path.join(LOGS_DIR, "deriv_session.json");

async function readSession() {
  try {
    const raw = await fs.readFile(SESSION_PATH, "utf8");
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export async function GET() {
  const session = await readSession();
  if (!session) {
    return NextResponse.json({ session_start_ts: null, session_start_iso: null });
  }
  return NextResponse.json(session);
}

export async function POST() {
  const now = Date.now();
  const payload = {
    session_start_ts: now,
    session_start_iso: new Date(now).toISOString(),
  };
  await fs.mkdir(path.dirname(SESSION_PATH), { recursive: true });
  await fs.writeFile(SESSION_PATH, JSON.stringify(payload, null, 2), "utf8");
  return NextResponse.json({ ok: true, ...payload });
}
