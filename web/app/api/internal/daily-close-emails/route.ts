import { NextRequest, NextResponse } from "next/server";
import { createHash, timingSafeEqual } from "crypto";
import fs from "node:fs/promises";
import path from "node:path";
import {
  listActiveClients,
  listClosedTradesSinceFechaInicio,
  type ClientCoreProfile,
} from "@/lib/clientData";
import { sendDailyCloseSummaryEmail } from "@/lib/email";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const SEND_LOG_FILE = "daily_close_email_log.json";

type SendLog = Record<string, string>;

function verifyBearer(authHeader: string, secret: string): boolean {
  const parts = authHeader.split(" ");
  if (parts.length !== 2 || parts[0] !== "Bearer") return false;
  try {
    const a = createHash("sha256").update(parts[1]).digest();
    const b = createHash("sha256").update(secret).digest();
    return timingSafeEqual(a, b);
  } catch {
    return false;
  }
}

function dayKey(iso: string): string {
  return iso.slice(0, 10);
}

function addDaysUtc(dayKeyUtc: string, deltaDays: number): string {
  const d = new Date(`${dayKeyUtc}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + deltaDays);
  return d.toISOString().slice(0, 10);
}

function resolveTargetDay(req: NextRequest): string {
  const q = req.nextUrl.searchParams.get("date");
  if (q && /^\d{4}-\d{2}-\d{2}$/.test(q)) return q;
  const today = new Date().toISOString().slice(0, 10);
  return addDaysUtc(today, -1);
}

function resolveLogsDir(): string {
  return process.env.DERIV_STATE_DIR
    ?? process.env.BOT_STATE_DIR
    ?? path.join(process.cwd(), "..", "logs");
}

async function readSendLog(logsDir: string): Promise<SendLog> {
  try {
    const raw = await fs.readFile(path.join(logsDir, SEND_LOG_FILE), "utf8");
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as SendLog;
    }
    return {};
  } catch {
    return {};
  }
}

async function writeSendLog(logsDir: string, data: SendLog): Promise<void> {
  await fs.mkdir(logsDir, { recursive: true });
  const finalPath = path.join(logsDir, SEND_LOG_FILE);
  const tempPath = `${finalPath}.tmp`;
  await fs.writeFile(tempPath, JSON.stringify(data, null, 2), "utf8");
  await fs.rename(tempPath, finalPath);
}

function buildDailySummary(client: ClientCoreProfile, trades: Array<{ closedAt: string; realizedPnl: number }>, targetDay: string) {
  const dayTrades = trades.filter((t) => dayKey(t.closedAt) === targetDay);
  if (!dayTrades.length) return null;

  const pnl = dayTrades.reduce((acc, t) => acc + t.realizedPnl, 0);
  const service = Math.max(pnl, 0) * 0.20;
  const clientNet = pnl - service;
  const pnlBeforeDay = trades
    .filter((t) => dayKey(t.closedAt) < targetDay)
    .reduce((acc, t) => acc + t.realizedPnl, 0);
  const capitalStart = client.capitalInicial + pnlBeforeDay;
  const capitalEnd = capitalStart + pnl;
  const firstDayPartial = client.fechaInicio?.toISOString().slice(0, 10) === targetDay;

  return {
    dayKeyUtc: targetDay,
    trades: dayTrades.length,
    pnl,
    service,
    clientNet,
    capitalStart,
    capitalEnd,
    firstDayPartial,
  };
}

export async function POST(req: NextRequest) {
  const secret = process.env.DAILY_CLOSE_EMAIL_SECRET ?? process.env.WEBHOOK_SECRET;
  if (!secret) {
    return NextResponse.json({ ok: false, error: "Secret no configurado." }, { status: 500 });
  }

  const authHeader = req.headers.get("authorization") ?? "";
  if (!verifyBearer(authHeader, secret)) {
    return NextResponse.json({ ok: false, error: "Unauthorized." }, { status: 401 });
  }

  const targetDay = resolveTargetDay(req);
  const dryRun = ["1", "true", "yes"].includes((req.nextUrl.searchParams.get("dryRun") ?? "").toLowerCase());
  const logsDir = resolveLogsDir();
  const sendLog = await readSendLog(logsDir);

  const clients = await listActiveClients();
  let sent = 0;
  let skipped = 0;
  let wouldSend = 0;
  const results: Array<Record<string, unknown>> = [];

  for (const c of clients) {
    if (!c.fechaInicio) {
      skipped += 1;
      results.push({ userId: c.id, email: c.email, status: "skipped", reason: "missing_fecha_inicio" });
      continue;
    }

    if (c.fechaInicio.toISOString().slice(0, 10) > targetDay) {
      skipped += 1;
      results.push({ userId: c.id, email: c.email, status: "skipped", reason: "not_started_yet" });
      continue;
    }

    const dedupeKey = `${targetDay}:${c.id}`;
    if (sendLog[dedupeKey]) {
      skipped += 1;
      results.push({ userId: c.id, email: c.email, status: "skipped", reason: "already_sent", sentAt: sendLog[dedupeKey] });
      continue;
    }

    const trades = await listClosedTradesSinceFechaInicio(c.fechaInicio, c.id);
    const summary = buildDailySummary(c, trades, targetDay);
    if (!summary) {
      skipped += 1;
      results.push({ userId: c.id, email: c.email, status: "skipped", reason: "no_trades_in_day" });
      continue;
    }

    if (dryRun) {
      wouldSend += 1;
      results.push({ userId: c.id, email: c.email, status: "dry_run", summary });
      continue;
    }

    try {
      await sendDailyCloseSummaryEmail(c.email, c.displayName || null, summary);
      sendLog[dedupeKey] = new Date().toISOString();
      sent += 1;
      results.push({ userId: c.id, email: c.email, status: "sent", summary });
    } catch (err) {
      skipped += 1;
      results.push({
        userId: c.id,
        email: c.email,
        status: "failed",
        reason: err instanceof Error ? err.message : "unknown_error",
      });
    }
  }

  if (!dryRun) {
    await writeSendLog(logsDir, sendLog);
  }

  return NextResponse.json({
    ok: true,
    targetDay,
    dryRun,
    totalClients: clients.length,
    sent,
    skipped,
    wouldSend,
    results,
  });
}
