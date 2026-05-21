import fs from "node:fs/promises";
import path from "node:path";

export const dynamic = "force-dynamic";

const ROOT = path.join(process.cwd(), "..");
const LOGS = process.env.BOT_STATE_DIR || path.join(ROOT, "logs");
const DERIV_LOGS = process.env.DERIV_STATE_DIR || LOGS;
const SPIKE_FILE = path.join(DERIV_LOGS, "deriv_spike_events.json");

export async function GET(request) {
  const { searchParams } = new URL(request.url);
  const symbol = searchParams.get("symbol") || null;
  const limit = Math.min(parseInt(searchParams.get("limit") || "200", 10), 2000);
  const since = parseFloat(searchParams.get("since") || "0");

  let spikes = [];
  try {
    const content = await fs.readFile(SPIKE_FILE, "utf8");
    spikes = JSON.parse(content);
  } catch {
    // file not yet created — return empty
  }

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
