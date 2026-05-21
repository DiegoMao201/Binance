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

// Match a spike to a closed contract: the contract must have been opened near the spike
// (within [-45s, +180s] window — trade triggered by spike).
function findBestClosedMatch(spike, contracts) {
  const symbol = spike?.symbol;
  const spikeTs = Number(spike?.ts);
  if (!symbol || !Number.isFinite(spikeTs)) return null;

  const directionToSide = spike?.direction === "UP"
    ? "MULTUP"
    : spike?.direction === "DOWN"
      ? "MULTDOWN"
      : null;

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

// Match a spike to an OPEN contract: the contract must have been open DURING the spike.
// No pre-window limit — an open trade may have started many minutes before the spike.
function findActiveOpenMatch(spike, openContracts) {
  const symbol = spike?.symbol;
  const spikeTs = Number(spike?.ts);
  if (!symbol || !Number.isFinite(spikeTs)) return null;

  // Find all open contracts for this symbol that were already open when the spike occurred.
  // Pick the most recently opened one (closest to spike ts from the left).
  let bestMatch = null;
  let bestOpenedAt = -Infinity;

  for (const contract of openContracts) {
    if ((contract?.symbol || null) !== symbol) continue;
    const openedAt = Number(contract?.opened_at_ts);
    if (!Number.isFinite(openedAt)) continue;
    // Contract must have been opened before OR at the spike timestamp
    if (openedAt > spikeTs + 5) continue; // allow 5s slack for open-order latency
    if (openedAt > bestOpenedAt) {
      bestOpenedAt = openedAt;
      bestMatch = contract;
    }
  }

  return bestMatch;
}

function deriveSpikeStatus(spike, closedContracts, openContracts) {
  if (spike?.bot_entered === true) return spike;

  const bestClosedMatch = findBestClosedMatch(spike, closedContracts || []);

  if (!bestClosedMatch) {
    const bestOpenMatch = findActiveOpenMatch(spike, openContracts || []);
    if (!bestOpenMatch) return spike;

    const spikeTs = Number(spike?.ts);
    const openedTs = Number(bestOpenMatch?.opened_at_ts);
    const lagSec = Number.isFinite(spikeTs) && Number.isFinite(openedTs)
      ? Number((openedTs - spikeTs).toFixed(1))
      : null;
    const entryTiming = lagSec == null
      ? null
      : lagSec > 0
        ? "late"
        : lagSec < 0
          ? "early"
          : "on_time";

    return {
      ...spike,
      bot_entered: true,
      block_reason: null,
      trade_result: "open",
      trade_status: "open",
      trade_contract_id: bestOpenMatch?.contract_id ?? null,
      trade_opened_at_ts: bestOpenMatch?.opened_at_ts ?? null,
      trade_closed_at_ts: null,
      entry_lag_sec: lagSec,
      entry_timing: entryTiming,
    };
  }

  const realizedPnl = Number(bestClosedMatch?.realized_pnl_usdt);
  const exitReason = String(bestClosedMatch?.exit_reason || "");
  const tradeWon = Number.isFinite(realizedPnl)
    ? realizedPnl > 0
    : /(^|_)won($|\b)|spike_tp/i.test(exitReason);
  const spikeTs = Number(spike?.ts);
  const openedTs = Number(bestClosedMatch?.opened_at_ts);
  const lagSec = Number.isFinite(spikeTs) && Number.isFinite(openedTs)
    ? Number((openedTs - spikeTs).toFixed(1))
    : null;
  const entryTiming = lagSec == null
    ? null
    : lagSec > 0
      ? "late"
      : lagSec < 0
        ? "early"
        : "on_time";

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
    entry_lag_sec: lagSec,
    entry_timing: entryTiming,
  };
}

function annotateSpikeSequence(spikes) {
  const byContract = new Map();

  for (const spike of spikes) {
    const cid = spike?.trade_contract_id;
    if (!cid) continue;
    if (!byContract.has(cid)) byContract.set(cid, []);
    byContract.get(cid).push(spike);
  }

  for (const [, list] of byContract.entries()) {
    list.sort((a, b) => Number(a?.ts || 0) - Number(b?.ts || 0));
    for (let i = 0; i < list.length; i += 1) {
      const seq = i + 1;
      list[i].spike_seq_for_trade = seq;
      list[i].spike_label = `spike_${seq}`;
    }
  }

  return spikes;
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
  spikes = annotateSpikeSequence(spikes);

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
