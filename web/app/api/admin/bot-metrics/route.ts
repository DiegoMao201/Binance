/**
 * GET /api/admin/bot-metrics?window=24h|7d|30d
 * Metricas agregadas del bot maestro (no del cliente):
 *  - PnL, trades, WR, PF, SL hits, timeout wins, ratchet
 * Lee de deriv_closed_contracts.json (fuente unica del bot Deriv).
 */

import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import fs from "node:fs/promises";
import path from "node:path";
import { resolveSessionFromCookies } from "@/lib/authSession";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const WINDOWS: Record<string, number> = {
  "24h": 24 * 3600 * 1000,
  "7d":  7 * 24 * 3600 * 1000,
  "30d": 30 * 24 * 3600 * 1000,
};

async function readClosedContracts(): Promise<Array<Record<string, unknown>>> {
  const logsDir = process.env.DERIV_STATE_DIR
    ?? process.env.BOT_STATE_DIR
    ?? path.join(process.cwd(), "..", "logs");
  try {
    const raw = await fs.readFile(path.join(logsDir, "deriv_closed_contracts.json"), "utf8");
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function toMs(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value > 1e12 ? value : value * 1000;
  }
  if (typeof value === "string" && value.trim()) {
    const n = Number(value);
    if (Number.isFinite(n)) return n > 1e12 ? n : n * 1000;
    const d = new Date(value);
    if (!Number.isNaN(d.getTime())) return d.getTime();
  }
  return null;
}

export async function GET(req: Request) {
  const cookieStore = await cookies();
  const session = await resolveSessionFromCookies(cookieStore, "admin");
  if (!session?.payload) {
    return NextResponse.json({ ok: false, error: "Acceso denegado." }, { status: 403 });
  }

  const url = new URL(req.url);
  const win = url.searchParams.get("window") ?? "24h";
  const windowMs = WINDOWS[win] ?? WINDOWS["24h"];
  const cutoff = Date.now() - windowMs;

  const all = await readClosedContracts();
  const inWindow = all.filter((r) => {
    const ms = toMs(r.closed_at_ts);
    return ms != null && ms >= cutoff;
  });

  let pnl = 0;
  let wins = 0;
  let losses = 0;
  let grossWin = 0;
  let grossLoss = 0;
  let slHits = 0;
  let timeoutWins = 0;
  let ratchet = 0;

  for (const r of inWindow) {
    const p = Number(r.realized_pnl_usdt ?? 0);
    if (!Number.isFinite(p)) continue;
    pnl += p;
    if (p > 0) { wins += 1; grossWin += p; }
    else if (p < 0) { losses += 1; grossLoss += -p; }
    const reason = String(r.exit_reason ?? "").toLowerCase();
    if (reason.includes("stop_loss") || reason === "sl") slHits += 1;
    if (reason.includes("timeout") && p > 0) timeoutWins += 1;
    if (reason.includes("ratchet") || reason.includes("trailing")) ratchet += 1;
  }

  const trades = inWindow.length;
  const wr = trades > 0 ? (wins / trades) * 100 : 0;
  const pf = grossLoss > 0 ? grossWin / grossLoss : (grossWin > 0 ? Infinity : 0);

  return NextResponse.json({
    ok: true,
    window: win,
    trades,
    pnl,
    wins,
    losses,
    wr,
    pf: Number.isFinite(pf) ? pf : null,
    slHits,
    timeoutWins,
    ratchet,
  });
}
