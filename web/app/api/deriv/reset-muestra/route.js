import fs from "node:fs/promises";
import path from "node:path";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const ROOT = path.join(process.cwd(), "..");
const LOGS = process.env.BOT_STATE_DIR || path.join(ROOT, "logs");
const DERIV_LOGS = process.env.DERIV_STATE_DIR || LOGS;

const SPIKE_FILE    = path.join(DERIV_LOGS, "deriv_spike_events.json");
const CLOSED_FILE   = path.join(DERIV_LOGS, "deriv_closed_contracts.json");
const ED_STATE_FILE = path.join(DERIV_LOGS, "entrada_diego_state.json");
const JSONL_FILE    = path.join(DERIV_LOGS, "deriv_ed_analysis.jsonl");
const ARCHIVE_DIR   = path.join(DERIV_LOGS, `archive_reset_${new Date().toISOString().slice(0,10)}`);

const K1000_SYMS = new Set(["CRASH900", "BOOM900", "CRASH1000", "BOOM1000"]);

async function readJson(file, fallback) {
  try { return JSON.parse(await fs.readFile(file, "utf8")); } catch { return fallback; }
}

export async function POST() {
  const ts = new Date().toISOString();
  const log = [];

  try {
    await fs.mkdir(ARCHIVE_DIR, { recursive: true });
    log.push(`Directorio de archivo: ${ARCHIVE_DIR}`);

    // 1. Filtrar spike events — conservar solo K1000
    try {
      const spikes = await readJson(SPIKE_FILE, []);
      const all = Array.isArray(spikes) ? spikes : [];
      const k1000 = all.filter((s) => K1000_SYMS.has(s.symbol));
      await fs.writeFile(
        path.join(ARCHIVE_DIR, "deriv_spike_events_backup.json"),
        JSON.stringify(all, null, 2)
      );
      await fs.writeFile(SPIKE_FILE, JSON.stringify(k1000, null, 2));
      log.push(`Spikes: ${all.length} total → ${k1000.length} K1000 conservados (${all.length - k1000.length} eliminados)`);
    } catch (e) { log.push(`ERROR spikes: ${e.message}`); }

    // 2. Archivar + limpiar contratos cerrados
    try {
      const closed = await readJson(CLOSED_FILE, []);
      await fs.writeFile(
        path.join(ARCHIVE_DIR, "deriv_closed_contracts_backup.json"),
        JSON.stringify(closed, null, 2)
      );
      await fs.writeFile(CLOSED_FILE, "[]");
      log.push(`Contratos cerrados: ${Array.isArray(closed) ? closed.length : 0} archivados → []`);
    } catch (e) { log.push(`ERROR contratos: ${e.message}`); }

    // 3. Archivar + limpiar JSONL de análisis
    try {
      const jsonl = await fs.readFile(JSONL_FILE, "utf8").catch(() => "");
      const lines = jsonl.split("\n").filter((l) => l.trim()).length;
      await fs.writeFile(path.join(ARCHIVE_DIR, "deriv_ed_analysis_backup.jsonl"), jsonl);
      await fs.writeFile(JSONL_FILE, "");
      log.push(`JSONL análisis: ${lines} registros archivados → vacío`);
    } catch (e) { log.push(`ERROR JSONL: ${e.message}`); }

    // 4. Resetear day_pnl y gates en entrada_diego_state.json
    try {
      const edState = await readJson(ED_STATE_FILE, {});
      const resetFields = {
        day_pnl_1000: 0.0,
        day_start_ts_1000: 0.0,
        k1000_win_gate_triggered: false,
        k1000_loss_gate_floor: 0.0,
        k1000_blocked_until: 0.0,
        k1000_rest_until: 0.0,
      };
      const resetted = {};
      for (const [sym, state] of Object.entries(edState)) {
        if (K1000_SYMS.has(sym) && state && typeof state === "object") {
          resetted[sym] = { ...state, ...resetFields };
        } else {
          resetted[sym] = state;
        }
      }
      await fs.writeFile(
        path.join(ARCHIVE_DIR, "entrada_diego_state_backup.json"),
        JSON.stringify(edState, null, 2)
      );
      await fs.writeFile(ED_STATE_FILE, JSON.stringify(resetted, null, 2));
      const symsReset = Object.keys(resetted).filter((s) => K1000_SYMS.has(s));
      log.push(`Estado bot: day_pnl=0, gates reset para ${symsReset.join(", ")}`);
    } catch (e) { log.push(`ERROR estado: ${e.message}`); }

    return NextResponse.json({ ok: true, ts, archive: ARCHIVE_DIR, log });
  } catch (e) {
    return NextResponse.json({ ok: false, error: e.message, log }, { status: 500 });
  }
}

// GET retorna el estado actual antes de resetear (preview)
export async function GET() {
  try {
    const spikes = await readJson(SPIKE_FILE, []);
    const closed = await readJson(CLOSED_FILE, []);
    const edState = await readJson(ED_STATE_FILE, {});

    const allSpikes = Array.isArray(spikes) ? spikes : [];
    const k1000Spikes = allSpikes.filter((s) => K1000_SYMS.has(s.symbol));
    const otherSpikes = allSpikes.length - k1000Spikes.length;

    const dayPnls = {};
    for (const sym of K1000_SYMS) {
      dayPnls[sym] = edState[sym]?.day_pnl_1000 ?? 0;
    }

    return NextResponse.json({
      preview: true,
      spikes_total: allSpikes.length,
      spikes_k1000: k1000Spikes.length,
      spikes_other_to_delete: otherSpikes,
      closed_contracts: Array.isArray(closed) ? closed.length : 0,
      day_pnl: dayPnls,
      message: "POST /api/deriv/reset-muestra para ejecutar el reset",
    });
  } catch (e) {
    return NextResponse.json({ error: e.message }, { status: 500 });
  }
}
