import fs from "node:fs/promises";
import path from "node:path";

export const dynamic = "force-dynamic";

const ROOT = path.join(process.cwd(), "..");
const LOGS = process.env.BOT_STATE_DIR || path.join(ROOT, "logs");
const DERIV_LOGS = process.env.DERIV_STATE_DIR || LOGS;
const SPIKE_FILE = path.join(DERIV_LOGS, "deriv_spike_events.json");
const CLOSED_FILE = path.join(DERIV_LOGS, "deriv_closed_contracts.json");
const OPEN_FILE = path.join(DERIV_LOGS, "deriv_open_contracts.json");

function parseJson(content, fallback) {
  try {
    return JSON.parse(content);
  } catch {
    return fallback;
  }
}

function findBestContractMatch(spike, contracts) {
  const symbol = spike?.symbol;
  const spikeTs = Number(spike?.ts);
  if (!symbol || !Number.isFinite(spikeTs)) return null;

  const directionToSide = spike?.direction === "UP"
    ? "MULTUP"
    : spike?.direction === "DOWN"
      ? "MULTDOWN"
      : null;

  // Some events are detected a few seconds after order placement.
  const preWindowSec = 45;
  const postWindowSec = 180;
  let bestMatch = null;
  let bestAbsDelta = Number.POSITIVE_INFINITY;

  for (const contract of contracts) {
    if ((contract?.symbol || null) !== symbol) continue;
    const openedAt = Number(contract?.opened_at_ts);
    if (!Number.isFinite(openedAt)) continue;
    const delta = openedAt - spikeTs;
    if (delta < -preWindowSec || delta > postWindowSec) continue;
    if (directionToSide && contract?.side && contract.side !== directionToSide) continue;

    const absDelta = Math.abs(delta);
    if (absDelta < bestAbsDelta) {
      bestAbsDelta = absDelta;
      bestMatch = contract;
    }
  }

  return bestMatch;
}

function deriveSpikeStatus(spike, closedContracts, openContracts) {
  if (spike?.bot_entered === true) return spike;

  const bestClosedMatch = findBestContractMatch(spike, closedContracts || []);

  if (!bestClosedMatch) {
    const bestOpenMatch = findBestContractMatch(spike, openContracts || []);
    if (!bestOpenMatch) return spike;

    return {
      ...spike,
      bot_entered: true,
      block_reason: null,
      trade_result: "open",
      trade_status: "open",
      trade_contract_id: bestOpenMatch?.contract_id ?? null,
      trade_opened_at_ts: bestOpenMatch?.opened_at_ts ?? null,
      trade_closed_at_ts: null,
    };
  }

  const realizedPnl = Number(bestClosedMatch?.realized_pnl_usdt);
  const exitReason = String(bestClosedMatch?.exit_reason || "");
  const tradeWon = Number.isFinite(realizedPnl)
    ? realizedPnl > 0
    : /(^|_)won($|\b)|spike_tp/i.test(exitReason);

  return {
    ...spike,
    bot_entered: true,
    block_reason: null,
    trade_result: tradeWon ? "win" : "loss",
    trade_status: "closed",
    trade_exit_reason: bestClosedMatch?.exit_reason || null,
    trade_contract_id: bestClosedMatch?.contract_id ?? null,
    trade_opened_at_ts: bestClosedMatch?.opened_at_ts ?? null,
    trade_closed_at_ts: bestClosedMatch?.closed_at_ts ?? null,
  };
}

export async function GET(request) {
  const { searchParams } = new URL(request.url);
  const symbol = searchParams.get("symbol") || null;
  const limit = Math.min(parseInt(searchParams.get("limit") || "200", 10), 2000);
  const since = parseFloat(searchParams.get("since") || "0");

  let spikes = [];
  let closedContracts = [];
  let openContracts = [];
  try {
    const content = await fs.readFile(SPIKE_FILE, "utf8");
    spikes = parseJson(content, []);
  } catch {
    // file not yet created — return empty
  }

  try {
    const content = await fs.readFile(CLOSED_FILE, "utf8");
    closedContracts = parseJson(content, []);
  } catch {
    // closed-contract history may not exist yet
  }

  try {
    const content = await fs.readFile(OPEN_FILE, "utf8");
    openContracts = parseJson(content, []);
  } catch {
    // open-contract state may not exist yet
  }

  spikes = spikes.map((spike) => deriveSpikeStatus(spike, closedContracts, openContracts));

  if (since > 0) spikes = spikes.filter((e) => e.ts >= since);
  if (symbol) spikes = spikes.filter((e) => e.symbol === symbol);
  spikes = spikes.slice(-limit);

  const format = searchParams.get("format") || "json";
  if (format === "csv") {
    const cols = ["ts","iso","symbol","direction","jump","atr","ratio","price","loss_streak","since_last_trade_s","lockout_active","bot_entered","block_reason","score","had_open_pos"];
    const rows = [cols.join(",")];
    for (const e of spikes) {
      rows.push(cols.map(c => {
        const v = e[c];
        if (v == null) return "";
        if (typeof v === "string" && v.includes(",")) return `"${v}"`;
        return v;
      }).join(","));
    }
    return new Response(rows.join("\n"), {
      headers: { "Content-Type": "text/csv", "Content-Disposition": "attachment; filename=deriv_spike_events.csv" },
    });
  }

  return Response.json({ total: spikes.length, spikes });
}
