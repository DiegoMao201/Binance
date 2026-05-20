import { NextResponse } from "next/server";
import fs from "node:fs/promises";
import path from "node:path";

export const dynamic = "force-dynamic";

const ROOT = path.join(process.cwd(), "..");
const LOGS = process.env.BOT_STATE_DIR || path.join(ROOT, "logs");
const DERIV_LOGS = process.env.DERIV_STATE_DIR || LOGS;

async function readJson(fileName, fallback) {
  try {
    const content = await fs.readFile(path.join(DERIV_LOGS, fileName), "utf8");
    return JSON.parse(content);
  } catch { return fallback; }
}

const tsOf = (c) => {
  const t = c?.closed_at_ts ?? c?.opened_at_ts ?? c?.ts ?? null;
  if (t == null) return null;
  return typeof t === "number" ? (t < 1e12 ? t * 1000 : t) : new Date(t).getTime();
};
const pnlOf = (c) => Number(c?.realized_pnl_usdt ?? c?.pnl ?? 0) || 0;

function toCSV(rows) {
  if (!rows || rows.length === 0) return "";
  // Flatten one level for nested objects (score_breakdown.*)
  const flat = rows.map(r => {
    const o = {};
    for (const [k, v] of Object.entries(r)) {
      if (v && typeof v === "object" && !Array.isArray(v)) {
        for (const [k2, v2] of Object.entries(v)) o[`${k}.${k2}`] = v2;
      } else {
        o[k] = v;
      }
    }
    return o;
  });
  const cols = Array.from(flat.reduce((s, r) => {
    Object.keys(r).forEach(k => s.add(k));
    return s;
  }, new Set()));
  const esc = (v) => {
    if (v == null) return "";
    const s = typeof v === "object" ? JSON.stringify(v) : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const head = cols.join(",");
  const body = flat.map(r => cols.map(c => esc(r[c])).join(",")).join("\n");
  return `${head}\n${body}\n`;
}

function applyFilters(rows, filters) {
  const { from, to, symbol, regime, strategy, side, min_score, max_score, result } = filters;
  return rows.filter(r => {
    const t = tsOf(r);
    if (from && t && t < Number(from)) return false;
    if (to && t && t > Number(to)) return false;
    if (symbol && (r.symbol || r.underlying) !== symbol) return false;
    if (regime && (r.regime || r.score_breakdown?.regime) !== regime) return false;
    if (strategy) {
      const bd = r.score_breakdown || {};
      const mode = bd.mean_rev_mode ? "mean_revert"
        : bd.fvg_active ? "smc"
        : bd.spike_entry ? "spike"
        : bd.micro_scalp ? "scalp" : "trend";
      if (mode !== strategy) return false;
    }
    if (side && r.side !== side) return false;
    const sc = Number(r.score ?? r.score_breakdown?.score_raw ?? 0);
    if (min_score != null && sc < Number(min_score)) return false;
    if (max_score != null && sc > Number(max_score)) return false;
    if (result === "win" && pnlOf(r) <= 0) return false;
    if (result === "loss" && pnlOf(r) >= 0) return false;
    return true;
  });
}

export async function GET(request) {
  const url = new URL(request.url);
  const dataset = (url.searchParams.get("dataset") || "trades").toLowerCase();
  const format = (url.searchParams.get("format") || "json").toLowerCase();

  const filters = {
    from: url.searchParams.get("from"),
    to: url.searchParams.get("to"),
    symbol: url.searchParams.get("symbol"),
    regime: url.searchParams.get("regime"),
    strategy: url.searchParams.get("strategy"),
    side: url.searchParams.get("side"),
    min_score: url.searchParams.get("min_score"),
    max_score: url.searchParams.get("max_score"),
    result: url.searchParams.get("result"),
  };

  // Phase-slice params: applied AFTER standard filters
  const lastN       = url.searchParams.get("last_n")    ? parseInt(url.searchParams.get("last_n"), 10)    : null;
  const sinceId     = url.searchParams.get("since_id")  ? parseInt(url.searchParams.get("since_id"), 10)  : null;
  const sinceSec    = url.searchParams.get("since_ts")  ? parseFloat(url.searchParams.get("since_ts"))    : null;  // unix seconds
  const sinceIso    = url.searchParams.get("since_date"); // ISO date string e.g. "2026-05-19"

  const [status, open, closed] = await Promise.all([
    readJson("deriv_status.json", {}),
    readJson("deriv_open_contracts.json", []),
    readJson("deriv_closed_contracts.json", []),
  ]);

  let rows = [];
  let filename = `deriv_${dataset}_${Date.now()}`;
  switch (dataset) {
    case "trades":
    case "closed":
      rows = applyFilters(Array.isArray(closed) ? closed : [], filters);
      break;
    case "open":
      rows = Array.isArray(open) ? open : [];
      break;
    case "decisions":
    case "telemetry":
      rows = applyFilters(Array.isArray(status?.last_decisions) ? status.last_decisions : [], filters);
      break;
    case "rejected":
      rows = applyFilters(Array.isArray(status?.last_decisions) ? status.last_decisions : [], filters)
        .filter(d => !d.allowed);
      break;
    case "equity":
      rows = Array.isArray(status?.equity_history) ? status.equity_history : [];
      break;
    default:
      return NextResponse.json({ error: `unknown dataset: ${dataset}` }, { status: 400 });
  }

  // ── Phase-slice filters (applied after base filters) ─────────────────────
  // since_id: only rows whose contract_id > since_id
  if (sinceId != null) {
    rows = rows.filter(r => {
      const id = parseInt(r.contract_id ?? r.id ?? 0, 10);
      return id > sinceId;
    });
  }
  // since_ts / since_date: only rows opened after the given time
  if (sinceSec != null || sinceIso != null) {
    const cutoffMs = sinceIso
      ? new Date(sinceIso).getTime()
      : sinceSec * 1000;
    rows = rows.filter(r => {
      const t = tsOf(r);
      return t != null && t >= cutoffMs;
    });
  }
  // last_n: keep only the N most-recent rows (by timestamp → stable order)
  if (lastN != null && lastN > 0 && rows.length > lastN) {
    // Sort desc by ts, take first N, restore asc order
    rows = rows
      .slice()
      .sort((a, b) => (tsOf(b) ?? 0) - (tsOf(a) ?? 0))
      .slice(0, lastN)
      .sort((a, b) => (tsOf(a) ?? 0) - (tsOf(b) ?? 0));
  }

  const sliceInfo = { last_n: lastN, since_id: sinceId, since_ts: sinceSec, since_date: sinceIso };

  if (format === "csv") {
    const csv = toCSV(rows);
    return new NextResponse(csv, {
      headers: {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": `attachment; filename="${filename}.csv"`,
        "Cache-Control": "no-store",
      },
    });
  }
  // JSON (default)
  return new NextResponse(JSON.stringify({
    dataset, count: rows.length, filters, slice: sliceInfo, rows,
    exported_at: new Date().toISOString(),
  }, null, 2), {
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Content-Disposition": `attachment; filename="${filename}.json"`,
      "Cache-Control": "no-store",
    },
  });
}
