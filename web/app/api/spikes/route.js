import fs from "node:fs/promises";
import path from "node:path";

export const dynamic = "force-dynamic";

const ROOT = path.join(process.cwd(), "..");
const LOGS = process.env.BOT_STATE_DIR || path.join(ROOT, "logs");
const SPIKE_FILE = path.join(LOGS, "deriv_spike_events.json");

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

  return Response.json({ total: spikes.length, spikes });
}
