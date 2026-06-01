import fs from "node:fs/promises";
import path from "node:path";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const ROOT = path.join(process.cwd(), "..");
const LOGS = process.env.BOT_STATE_DIR || path.join(ROOT, "logs");
const DERIV_LOGS = process.env.DERIV_STATE_DIR || LOGS;

const SPIKE_FILE = path.join(DERIV_LOGS, "deriv_spike_events.json");
const OPEN_FILE = path.join(DERIV_LOGS, "deriv_open_contracts.json");
const CLOSED_FILE = path.join(DERIV_LOGS, "deriv_closed_contracts.json");
const STATUS_FILE = path.join(DERIV_LOGS, "deriv_status.json");
const SESSION_FILE = path.join(DERIV_LOGS, "deriv_session.json");

async function readJson(file, fallback) {
  try {
    return JSON.parse(await fs.readFile(file, "utf8"));
  } catch {
    return fallback;
  }
}

const HOUR = 3600;

function median(arr) {
  if (!arr.length) return null;
  const s = [...arr].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

function quantile(arr, q) {
  if (!arr.length) return null;
  const s = [...arr].sort((a, b) => a - b);
  const pos = (s.length - 1) * q;
  const base = Math.floor(pos);
  const rest = pos - base;
  return s[base + 1] !== undefined ? s[base] + rest * (s[base + 1] - s[base]) : s[base];
}

// Readiness state from elapsed-vs-typical-interval ratio.
function readiness(ratio) {
  if (ratio == null) return { state: "SIN_DATOS", level: 0 };
  if (ratio < 0.5) return { state: "FRESCO", level: 1 };       // acaba de disparar
  if (ratio < 0.9) return { state: "CARGANDO", level: 2 };     // acumulando
  if (ratio < 1.4) return { state: "LISTO", level: 3 };        // ripe — vigilar
  if (ratio < 2.2) return { state: "VENCIDO", level: 4 };      // overdue — alta probabilidad
  return { state: "SECO", level: 5 };                          // muy pasado / mercado lento
}

export async function GET() {
  const nowSec = Date.now() / 1000;

  const [spikesRaw, openRaw, closedRaw, status, session] = await Promise.all([
    readJson(SPIKE_FILE, []),
    readJson(OPEN_FILE, []),
    readJson(CLOSED_FILE, []),
    readJson(STATUS_FILE, {}),
    readJson(SESSION_FILE, null),
  ]);

  const spikes = (Array.isArray(spikesRaw) ? spikesRaw : [])
    .filter((s) => s && Number.isFinite(Number(s.ts)))
    .map((s) => ({ ...s, ts: Number(s.ts) }))
    .sort((a, b) => a.ts - b.ts);

  const openContracts = Array.isArray(openRaw) ? openRaw : [];
  const closedContracts = Array.isArray(closedRaw) ? closedRaw : [];

  // Discover symbols dynamically from live data (spikes + open + recent closed).
  const symbolSet = new Set();
  for (const s of spikes) if (s.symbol) symbolSet.add(s.symbol);
  for (const c of openContracts) if (c.symbol) symbolSet.add(c.symbol);
  for (const c of closedContracts) if (c.symbol) symbolSet.add(c.symbol);

  const openBySymbol = new Map();
  for (const c of openContracts) {
    if (c.symbol) openBySymbol.set(c.symbol, c);
  }

  const symbols = [];
  for (const symbol of symbolSet) {
    const sym = spikes.filter((s) => s.symbol === symbol);
    if (!sym.length && !openBySymbol.has(symbol)) continue;

    const last = sym.length ? sym[sym.length - 1] : null;
    const secsSince = last ? nowSec - last.ts : null;

    // Hide stale symbols (no spike in 24h and no open position) to keep the
    // console focused on what the bot is actually trading right now.
    const active24 = sym.some((s) => nowSec - s.ts <= 24 * HOUR);
    if (!active24 && !openBySymbol.has(symbol)) continue;

    const count = (winSec) => sym.filter((s) => nowSec - s.ts <= winSec).length;
    const c1 = count(1 * HOUR);
    const c6 = count(6 * HOUR);
    const c12 = count(12 * HOUR);
    const c24 = count(24 * HOUR);

    // Gaps between consecutive spikes within last 24h → typical interval.
    const recent24 = sym.filter((s) => nowSec - s.ts <= 24 * HOUR);
    const gaps = [];
    for (let i = 1; i < recent24.length; i++) gaps.push(recent24[i].ts - recent24[i - 1].ts);
    const medianGap = median(gaps);
    const ratio = medianGap && secsSince != null ? secsSince / medianGap : null;
    const ready = readiness(ratio);

    const entered24 = recent24.filter((s) => s.bot_entered === true).length;
    const blocked24 = recent24.filter((s) => s.bot_entered === false).length;

    // Top block reasons (the "notes" per symbol).
    const reasonCounts = {};
    for (const s of recent24) {
      if (s.bot_entered === false && s.block_reason) {
        const key = String(s.block_reason).split(":")[0];
        reasonCounts[key] = (reasonCounts[key] || 0) + 1;
      }
    }
    const topReasons = Object.entries(reasonCounts)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 4)
      .map(([reason, n]) => ({ reason, n }));

    symbols.push({
      symbol,
      lastSpike: last
        ? {
            ts: last.ts,
            iso: last.iso,
            direction: last.direction,
            ratio: last.ratio,
            jump: last.jump,
            price: last.price,
            ticks_since_last_spike: last.ticks_since_last_spike,
            price_vs_ema200_pct: last.price_vs_ema200_pct,
            bot_entered: last.bot_entered,
            block_reason: last.block_reason,
            had_open_pos: last.had_open_pos,
            spike_cluster: last.spike_cluster,
          }
        : null,
      secsSinceLastSpike: secsSince,
      counts: { h1: c1, h6: c6, h12: c12, h24: c24 },
      ratePerHour: {
        h6: c6 / 6,
        h12: c12 / 12,
        h24: c24 / 24,
      },
      medianGapSec: medianGap,
      p75GapSec: quantile(gaps, 0.75),
      readiness: ready,
      entered24,
      blocked24,
      catchRate24: c24 ? entered24 / c24 : null,
      topReasons,
      openContract: openBySymbol.get(symbol) || null,
      recentSpikes: sym.slice(-12).reverse(),
    });
  }

  // Sort: symbols with an open position first, then by readiness level desc, then by activity.
  symbols.sort((a, b) => {
    const ao = a.openContract ? 1 : 0;
    const bo = b.openContract ? 1 : 0;
    if (ao !== bo) return bo - ao;
    if (b.readiness.level !== a.readiness.level) return b.readiness.level - a.readiness.level;
    return b.counts.h24 - a.counts.h24;
  });

  // Recent entries (bot operations) — open + last closed, with timestamps.
  const recentEntries = [];
  for (const c of openContracts) {
    recentEntries.push({
      status: "OPEN",
      symbol: c.symbol,
      side: c.side,
      stake: c.stake_usdt,
      entry_price: c.entry_price,
      opened_at_ts: c.opened_at_ts,
      floating_pnl: c.floating_pnl,
      peak_profit: c.peak_profit,
      duration_sec: c.duration_sec,
      contract_id: c.contract_id,
    });
  }
  const closedSorted = [...closedContracts]
    .filter((c) => Number.isFinite(Number(c.closed_at_ts)))
    .sort((a, b) => Number(b.closed_at_ts) - Number(a.closed_at_ts))
    .slice(0, 25);
  for (const c of closedSorted) {
    recentEntries.push({
      status: "CLOSED",
      symbol: c.symbol,
      side: c.side,
      stake: c.stake_usdt,
      entry_price: c.entry_price,
      exit_price: c.exit_price,
      opened_at_ts: c.opened_at_ts,
      closed_at_ts: c.closed_at_ts,
      realized_pnl: c.realized_pnl_usdt,
      exit_reason: c.exit_reason,
      duration_sec: c.duracion_real_seg,
      grade: c.execution_grade,
      contract_id: c.contract_id,
    });
  }

  // Global spike feed (newest first).
  const spikeFeed = spikes.slice(-40).reverse().map((s) => ({
    ts: s.ts,
    iso: s.iso,
    symbol: s.symbol,
    direction: s.direction,
    ratio: s.ratio,
    jump: s.jump,
    bot_entered: s.bot_entered,
    block_reason: s.block_reason,
    had_open_pos: s.had_open_pos,
    secsAgo: nowSec - s.ts,
  }));

  // Session-level realized PnL (today) for the header.
  let sinceSec = 0;
  if (session && Number(session.session_start_ts)) {
    const ts = Number(session.session_start_ts);
    sinceSec = ts > 1e12 ? ts / 1000 : ts;
  }
  const sessionClosed = closedContracts.filter(
    (c) => !sinceSec || Number(c.closed_at_ts) >= sinceSec
  );
  const sessionPnl = sessionClosed.reduce(
    (acc, c) => acc + (Number(c.realized_pnl_usdt) || 0),
    0
  );
  const sessionWins = sessionClosed.filter((c) => Number(c.realized_pnl_usdt) > 0).length;

  return NextResponse.json(
    {
      serverTime: new Date().toISOString(),
      serverTs: nowSec,
      status: {
        state: status?.state || status?.status || null,
        message: status?.message || null,
        balance: status?.balance ?? status?.equity ?? null,
      },
      session: {
        sinceSec,
        realizedPnl: sessionPnl,
        trades: sessionClosed.length,
        wins: sessionWins,
        winRate: sessionClosed.length ? sessionWins / sessionClosed.length : null,
      },
      totals: {
        symbols: symbols.length,
        openPositions: openContracts.length,
        spikes24h: spikes.filter((s) => nowSec - s.ts <= 24 * HOUR).length,
      },
      symbols,
      openContracts,
      recentEntries,
      spikeFeed,
    },
    { headers: { "Cache-Control": "no-store, max-age=0" } }
  );
}
