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

// Match a spike to a contract (open or closed).
// Two cases:
//   1. Spike triggered the trade: contract opened within [-45s, +180s] of spike
//   2. Trade was active during the spike: opened_at <= spike_ts <= closed_at (or still open)
// We prefer case-1 matches (narrower window = more intentional). Case-2 handles long-running
// trades that were already open when a new spike occurred.
function findBestContractMatch(spike, contracts, isOpen) {
  const symbol = spike?.symbol;
  const spikeTs = Number(spike?.ts);
  if (!symbol || !Number.isFinite(spikeTs)) return null;

  const directionToSide = spike?.direction === "UP"
    ? "MULTUP"
    : spike?.direction === "DOWN"
      ? "MULTDOWN"
      : null;

  let triggeredMatch = null;
  let triggeredBestDelta = Number.POSITIVE_INFINITY;
  let activeMatch = null;
  let activeBestOpenedAt = -Infinity;

  for (const contract of contracts) {
    if ((contract?.symbol || null) !== symbol) continue;
    const openedAt = Number(contract?.opened_at_ts);
    if (!Number.isFinite(openedAt)) continue;
    if (directionToSide && contract?.side && contract.side !== directionToSide) continue;

    const delta = openedAt - spikeTs;

    // Case 1: spike triggered this trade (opened near the spike)
    if (delta >= -45 && delta <= 180) {
      const absDelta = Math.abs(delta);
      if (absDelta < triggeredBestDelta) {
        triggeredBestDelta = absDelta;
        triggeredMatch = contract;
      }
    }

    // Case 2: trade was already open when spike occurred
    //   For open contracts: opened before spike (still running, no closed_at)
    //   For closed contracts: opened before spike AND closed after spike
    if (openedAt <= spikeTs + 5) {
      const closedAt = isOpen ? null : Number(contract?.closed_at_ts ?? null);
      const wasActiveAtSpike = isOpen
        ? true  // still open = still active
        : Number.isFinite(closedAt) && closedAt >= spikeTs - 5;
      if (wasActiveAtSpike && openedAt > activeBestOpenedAt) {
        activeBestOpenedAt = openedAt;
        activeMatch = contract;
      }
    }
  }

  // Prefer case-1 (spike-triggered) over case-2 (was already open)
  return triggeredMatch || activeMatch;
}

function deriveSpikeStatus(spike, closedContracts, openContracts) {
  if (spike?.bot_entered === true) return spike;

  const bestClosedMatch = findBestContractMatch(spike, closedContracts || [], false);

  if (!bestClosedMatch) {
    const bestOpenMatch = findBestContractMatch(spike, openContracts || [], true);
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
