// SSE event-driven: push solo cuando los archivos del bot cambian (chokidar).
// 0 latencia · 0 polling · sin tocar el main_loop del bot.
// Compatible con clientes legacy (`onmessage`) y nuevos (`addEventListener('state')`).
import path from "node:path";
import chokidar from "chokidar";
import { readDashboardState } from "../../../lib/read-dashboard-state";

export const dynamic = "force-dynamic";

const ROOT = path.join(/*turbopackIgnore: true*/ process.cwd(), "..");
const LOGS = process.env.BOT_STATE_DIR || path.join(/*turbopackIgnore: true*/ ROOT, "logs");

const WATCHED = [
  "status.json",
  "bot_state.json",
  "open_positions.json",
  "closed_trades.json",
  "signal_history.json",
  "scan_history.json",
  "trade_monitor_log.json",
  "control.json",
  "pre_flight.json",
  "equity_history.json",
  "recovery_status.json",
];

export async function GET() {
  const encoder = new TextEncoder();

  const stream = new ReadableStream({
    async start(controller) {
      let closed = false;
      let lastPushAt = 0;
      let pendingTimer = null;
      const minIntervalMs = 250; // burst-control: máx 4 push/s

      const send = async () => {
        if (closed) return;
        try {
          const data = await readDashboardState();
          const json = JSON.stringify(data);
          // Doble emisión: `event: state` para clientes nuevos, default para `onmessage`.
          controller.enqueue(encoder.encode(`event: state\ndata: ${json}\n\n`));
          controller.enqueue(encoder.encode(`data: ${json}\n\n`));
          lastPushAt = Date.now();
        } catch (err) {
          // BUG FIX: was silent — log so SSE serialization errors surface in server logs.
          console.error("[stream/route] readDashboardState error:", err?.message || err);
        }
      };

      const schedule = () => {
        if (closed || pendingTimer) return;
        const elapsed = Date.now() - lastPushAt;
        const wait = Math.max(0, minIntervalMs - elapsed);
        pendingTimer = setTimeout(async () => {
          pendingTimer = null;
          await send();
        }, wait);
      };

      await send();

      const watcher = chokidar.watch(
        WATCHED.map((f) => path.join(LOGS, f)),
        { ignoreInitial: true, awaitWriteFinish: { stabilityThreshold: 80, pollInterval: 30 } }
      );
      watcher.on("change", schedule).on("add", schedule);

      const heartbeat = setInterval(() => {
        if (!closed) {
          try { controller.enqueue(encoder.encode(": ping\n\n")); } catch { /* closed */ }
        }
      }, 25000);

      const safetyResync = setInterval(schedule, 30000);

      return () => {
        closed = true;
        clearInterval(heartbeat);
        clearInterval(safetyResync);
        if (pendingTimer) clearTimeout(pendingTimer);
        watcher.close().catch(() => {});
      };
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}
