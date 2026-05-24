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
const DERIV_LOGS_DIR = process.env.DERIV_STATE_DIR || LOGS_DIR;
const SESSION_PATH = path.join(LOGS_DIR, "deriv_session.json");
const DERIV_SESSION_PATH = path.join(DERIV_LOGS_DIR, "deriv_session.json");
const MAX_FUTURE_SESSION_DRIFT_MS = 24 * 60 * 60 * 1000;

function normalizeSessionTs(value) {
  const ts = Number(value || 0);
  if (!Number.isFinite(ts) || ts <= 0) return 0;
  const tsMs = ts > 1e12 ? ts : ts * 1000;
  if (tsMs > Date.now() + MAX_FUTURE_SESSION_DRIFT_MS) return 0;
  return tsMs;
}

async function readSessionFile(filePath) {
  try {
    const raw = await fs.readFile(filePath, "utf8");
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

async function readSession() {
  const paths = [...new Set([SESSION_PATH, DERIV_SESSION_PATH])];
  let best = null;
  let bestTs = 0;

  for (const filePath of paths) {
    const session = await readSessionFile(filePath);
    if (!session) continue;
    const tsMs = normalizeSessionTs(session.session_start_ts);
    if (tsMs > bestTs) {
      bestTs = tsMs;
      best = {
        session_start_ts: tsMs,
        session_start_iso: new Date(tsMs).toISOString(),
      };
    }
  }

  return best;
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

  const paths = [...new Set([SESSION_PATH, DERIV_SESSION_PATH])];
  await Promise.all(
    paths.map(async (filePath) => {
      await fs.mkdir(path.dirname(filePath), { recursive: true });
      await fs.writeFile(filePath, JSON.stringify(payload, null, 2), "utf8");
    }),
  );

  return NextResponse.json({ ok: true, ...payload });
}
