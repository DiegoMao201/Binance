/**
 * GET /api/admin/symbol-metrics?window=24h|7d|30d
 * Agrega metricas por simbolo en la ventana indicada.
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

function toMs(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value > 1e12 ? value : value * 1000;
  if (typeof value === "string" && value.trim()) {
    const n = Number(value);
    if (Number.isFinite(n)) return n > 1e12 ? n : n * 1000;
    const d = new Date(value);
    if (!Number.isNaN(d.getTime())) return d.getTime();
  }
  return null;
}

async function readClosed(): Promise<Array<Record<string, unknown>>> {
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

export async function GET(req: Request) {
  const cookieStore = await cookies();
  const session = await resolveSessionFromCookies(cookieStore, "admin");
  if (!session?.payload) {
    return NextResponse.json({ ok: false, error: "Acceso denegado." }, { status: 403 });
  }

  const url = new URL(req.url);
  const win = url.searchParams.get("window") ?? "24h";
  const cutoff = Date.now() - (WINDOWS[win] ?? WINDOWS["24h"]);

  const all = await readClosed();
  type Agg = { trades: number; wins: number; pnl: number; sl: number };
  const bySym = new Map<string, Agg>();
  for (const r of all) {
    const ms = toMs(r.closed_at_ts);
    if (ms == null || ms < cutoff) continue;
    const sym = typeof r.symbol === "string" ? r.symbol : "DERIV";
    const p = Number(r.realized_pnl_usdt ?? 0);
    if (!Number.isFinite(p)) continue;
    const a = bySym.get(sym) ?? { trades: 0, wins: 0, pnl: 0, sl: 0 };
    a.trades += 1;
    a.pnl += p;
    if (p > 0) a.wins += 1;
    const reason = String(r.exit_reason ?? "").toLowerCase();
    if (reason.includes("stop_loss") || reason === "sl") a.sl += 1;
    bySym.set(sym, a);
  }

  const symbols = Array.from(bySym.entries())
    .map(([symbol, a]) => ({
      symbol,
      trades: a.trades,
      wr: a.trades > 0 ? (a.wins / a.trades) * 100 : 0,
      pnl: a.pnl,
      slPct: a.trades > 0 ? (a.sl / a.trades) * 100 : 0,
    }))
    .sort((a, b) => b.pnl - a.pnl);

  return NextResponse.json({ ok: true, window: win, symbols });
}
