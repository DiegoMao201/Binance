"use client";
/**
 * AdminDashboardView — Componente cliente que orquesta polling y rendering
 * de las 5 secciones del frontend admin.
 *
 * Polling:
 *  - bot status: 5s
 *  - clientes (incluye lectura Deriv WS server-side por cliente): 15s
 *  - posiciones abiertas: 5s
 *  - metricas + por simbolo: cuando cambia la ventana
 */

import { useEffect, useMemo, useState, useTransition } from "react";

const BG    = "#050b12";
const CARD  = "rgba(8,15,25,0.78)";
const BORD  = "rgba(76,136,170,0.24)";
const TEXT  = "#e6f2ff";
const MUTE  = "#8aa5bf";
const GREEN = "#19c37d";
const RED   = "#ff6b6b";
const AMBER = "#ffbf47";
const BLUE  = "#3dd6ff";

type BotStatus = {
  ok?: boolean;
  status?: string;
  connected?: boolean;
  heartbeatAt?: string | null;
  heartbeatAgeSec?: number | null;
  ordersSent?: number;
  ordersOk?: number;
  ticksTotal?: number;
  activeSymbols?: number;
  openContracts?: number;
};

type ClientRow = {
  id: string;
  displayName: string;
  email: string;
  derivAccountId: string | null;
  hasDerivToken: boolean;
  fechaInicio: string | null;
  capitalInicial: number;
  balanceActual: number | null;
  balanceSource: "deriv_ws" | "cache" | "unavailable";
  balanceError?: string;
  gananciaNeta: number | null;
  rendimientoPct: number | null;
  enModoRecuperacion: boolean | null;
  mensajeEstado: string;
  adminFee20: number;
  adminFee20LatestDay: number;
  adminFee20TotalEstimated: number;
  realizedPnlTotal: number;
  realizedPnlTodayUtc: number;
  latestDayKey: string | null;
  latestDayPnl: number;
  tradesSinceStart: number;
};

type ClientsResponse = {
  ok: boolean;
  clients?: ClientRow[];
  totalAdminFee20?: number;
  totalAdminFee20LatestDay?: number;
  totalAdminFee20Estimated?: number;
  totalRealizedPnl?: number;
  count?: number;
};

type OpenPos = { symbol: string; stake: number; currentPnl: number; openedAt: string | null; contractId: unknown };
type OpenResp = { ok: boolean; positions?: OpenPos[]; count?: number };

type BotMetrics = {
  ok?: boolean;
  window?: string;
  trades?: number;
  pnl?: number;
  wins?: number;
  losses?: number;
  wr?: number;
  pf?: number | null;
  slHits?: number;
  timeoutWins?: number;
  ratchet?: number;
};

type SymMetrics = {
  ok?: boolean;
  window?: string;
  symbols?: Array<{ symbol: string; trades: number; wr: number; pnl: number; slPct: number }>;
};

function fmtUSD(n: number, sign = false): string {
  const prefix = n < 0 ? "-" : sign && n > 0 ? "+" : "";
  return `${prefix}$${Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtMaybeUSD(n: number | null, sign = false): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return fmtUSD(n, sign);
}

export function AdminDashboardView() {
  const [bot, setBot] = useState<BotStatus | null>(null);
  const [clients, setClients] = useState<ClientsResponse | null>(null);
  const [openPos, setOpenPos] = useState<OpenResp | null>(null);
  const [windowSel, setWindowSel] = useState<"24h" | "7d" | "30d">("24h");
  const [metrics, setMetrics] = useState<BotMetrics | null>(null);
  const [symMetrics, setSymMetrics] = useState<SymMetrics | null>(null);
  const [showAdd, setShowAdd] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function pull() {
      try {
        const [s, op] = await Promise.all([
          fetch("/api/admin/bot-status", { cache: "no-store" }).then((r) => r.json()),
          fetch("/api/admin/open-positions", { cache: "no-store" }).then((r) => r.json()),
        ]);
        if (!cancelled) { setBot(s); setOpenPos(op); }
      } catch { /* noop */ }
    }
    pull();
    const id = setInterval(pull, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function pull() {
      try {
        const c = await fetch("/api/admin/clients", { cache: "no-store" }).then((r) => r.json());
        if (!cancelled) setClients(c);
      } catch { /* noop */ }
    }
    pull();
    const id = setInterval(pull, 15_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function pull() {
      try {
        const [m, s] = await Promise.all([
          fetch(`/api/admin/bot-metrics?window=${windowSel}`, { cache: "no-store" }).then((r) => r.json()),
          fetch(`/api/admin/symbol-metrics?window=${windowSel}`, { cache: "no-store" }).then((r) => r.json()),
        ]);
        if (!cancelled) { setMetrics(m); setSymMetrics(s); }
      } catch { /* noop */ }
    }
    pull();
    const id = setInterval(pull, 30_000);
    return () => { cancelled = true; clearInterval(id); };
  }, [windowSel]);

  function reloadClients() {
    fetch("/api/admin/clients", { cache: "no-store" })
      .then((r) => r.json())
      .then((c) => setClients(c))
      .catch(() => { /* noop */ });
  }

  return (
    <div style={{
      minHeight: "100vh",
      background: [
        "radial-gradient(900px 600px at 8% -10%, rgba(25,195,125,0.10), transparent 62%)",
        "radial-gradient(760px 460px at 95% 12%, rgba(61,214,255,0.10), transparent 65%)",
        "linear-gradient(180deg, #060d17 0%, #050b12 100%)",
      ].join(", "),
      color: TEXT,
      fontFamily: "Sora, Manrope, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
      padding: 24,
      boxSizing: "border-box",
    }}>
      <Header />

      {/* Seccion 1 — Estado del bot */}
      <BotStatusPanel bot={bot} openCount={openPos?.count ?? 0} />

      {/* Seccion 2 — Tabla de clientes */}
      <ClientsPanel clients={clients} onAdd={() => setShowAdd(true)} />

      {/* Seccion 3 — Posiciones abiertas del bot maestro */}
      <OpenPositionsPanel positions={openPos?.positions ?? []} />

      {/* Seccion 4 — Metricas del bot */}
      <BotMetricsPanel metrics={metrics} windowSel={windowSel} onChangeWindow={setWindowSel} />

      {/* Seccion 5 — Por simbolo */}
      <SymbolMetricsPanel data={symMetrics} />

      {showAdd && (
        <AddClientModal onClose={() => setShowAdd(false)} onCreated={() => { setShowAdd(false); reloadClients(); }} />
      )}
    </div>
  );
}

function Header() {
  return (
    <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
      <div>
        <h1 style={{ fontSize: 22, fontWeight: 800, margin: 0, letterSpacing: "-0.02em" }}>
          ◈ OptiFerre <span style={{ color: GREEN }}>Mission Control</span>
        </h1>
        <p style={{ color: MUTE, fontSize: 11, margin: "4px 0 0", letterSpacing: "0.08em" }}>
          ADMIN · liquidacion transparente
        </p>
      </div>
    </header>
  );
}

function BotStatusPanel({ bot, openCount }: { bot: BotStatus | null; openCount: number }) {
  const ok = bot?.connected && (bot?.status ?? "").toLowerCase() === "running";
  return (
    <Card title="Estado del bot" accent={ok ? GREEN : AMBER}>
      <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "center" }}>
        <Badge color={ok ? GREEN : AMBER} text={`● ${(bot?.status ?? "UNKNOWN").toUpperCase()}`} />
        <Badge color={bot?.connected ? GREEN : MUTE} text={bot?.connected ? "Conectado" : "Sin conexion"} />
        <span style={{ color: MUTE, fontSize: 12 }}>
          Heartbeat: {bot?.heartbeatAgeSec != null ? `hace ${bot.heartbeatAgeSec}s` : "—"}
        </span>
        <span style={{ color: MUTE, fontSize: 12 }}>
          Orders OK: <span style={{ color: TEXT }}>{bot?.ordersOk ?? 0}/{bot?.ordersSent ?? 0}</span>
        </span>
        <span style={{ color: MUTE, fontSize: 12 }}>
          Simbolos activos: <span style={{ color: TEXT }}>{bot?.activeSymbols ?? 0}</span>
        </span>
        <span style={{ color: MUTE, fontSize: 12 }}>
          Contratos abiertos: <span style={{ color: TEXT }}>{openCount}</span>
        </span>
      </div>
    </Card>
  );
}

function ClientsPanel({ clients, onAdd }: { clients: ClientsResponse | null; onAdd: () => void }) {
  const rows = clients?.clients ?? [];
  const totalLatestDay = clients?.totalAdminFee20LatestDay ?? clients?.totalAdminFee20 ?? 0;
  const totalEstimated = clients?.totalAdminFee20Estimated ?? 0;
  const totalRealizedPnl = clients?.totalRealizedPnl ?? 0;
  return (
    <Card
      title="Clientes"
      accent={BLUE}
      action={
        <button onClick={onAdd} style={{
          background: GREEN,
          color: "#000",
          border: "none",
          borderRadius: 8,
          padding: "8px 14px",
          fontWeight: 700,
          cursor: "pointer",
          fontSize: 12,
        }}>+ Anadir Cliente</button>
      }
    >
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 10, marginBottom: 12 }}>
        <MiniKpi label="Clientes activos" value={String(rows.length)} color={BLUE} />
        <MiniKpi label="PnL real acumulado" value={fmtUSD(totalRealizedPnl, true)} color={totalRealizedPnl >= 0 ? GREEN : RED} />
        <MiniKpi label="Cobro ultimo cierre (20%)" value={fmtUSD(totalLatestDay)} color={AMBER} />
        <MiniKpi label="Cobro acumulado estimado" value={fmtUSD(totalEstimated)} color={AMBER} />
      </div>

      {rows.length === 0 ? (
        <p style={{ color: MUTE }}>Sin clientes activos.</p>
      ) : (
        <Table headers={["Cliente", "Capital", "Balance live", "PnL real desde inicio", "Ultimo cierre", "Cobro 20% ultimo cierre", "Estado"]}>
          {rows.map((c) => (
            <tr key={c.id}>
              <Td>
                <div style={{ display: "flex", flexDirection: "column" }}>
                  <span>{c.displayName || c.email}</span>
                  <span style={{ color: MUTE, fontSize: 10 }}>{c.derivAccountId ?? "sin Deriv"} · {c.balanceSource}</span>
                  <span style={{ color: MUTE, fontSize: 10 }}>
                    conteo desde {c.fechaInicio ? new Date(c.fechaInicio).toLocaleString("es-ES", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "sin fecha_inicio"}
                  </span>
                  {c.balanceError && <span style={{ color: AMBER, fontSize: 10 }}>{c.balanceError}</span>}
                </div>
              </Td>
              <Td mono>{fmtUSD(c.capitalInicial)}</Td>
              <Td mono color={c.balanceSource === "deriv_ws" ? TEXT : c.balanceSource === "cache" ? MUTE : AMBER}>
                {fmtMaybeUSD(c.balanceActual)}
              </Td>
              <Td mono color={c.realizedPnlTotal >= 0 ? GREEN : RED}>
                {fmtUSD(c.realizedPnlTotal, true)}
              </Td>
              <Td>
                <div style={{ display: "flex", flexDirection: "column" }}>
                  <span style={{ color: MUTE, fontSize: 10 }}>
                    {c.latestDayKey ? new Date(`${c.latestDayKey}T00:00:00Z`).toLocaleDateString("es-ES") : "Sin cierres"}
                  </span>
                  <span style={{ color: c.latestDayPnl >= 0 ? GREEN : RED, fontFamily: "ui-monospace, Menlo, monospace" }}>
                    {fmtUSD(c.latestDayPnl, true)}
                  </span>
                </div>
              </Td>
              <Td mono color={c.enModoRecuperacion == null ? MUTE : c.enModoRecuperacion ? MUTE : AMBER}>
                {c.enModoRecuperacion == null ? "—" : c.enModoRecuperacion ? fmtUSD(0) : fmtUSD(c.adminFee20LatestDay)}
              </Td>
              <Td color={c.enModoRecuperacion == null ? MUTE : c.enModoRecuperacion ? AMBER : GREEN}>
                {c.enModoRecuperacion == null ? "Sin lectura Deriv" : c.enModoRecuperacion ? "Recuperacion" : "En ganancia"}
              </Td>
            </tr>
          ))}
        </Table>
      )}
      <p style={{ marginTop: 12, color: MUTE, fontSize: 12, lineHeight: 1.5 }}>
        Regla de cobro visible para todo el equipo: 20% sobre el PnL neto positivo del ultimo dia cerrado.
        No se toma como base el saldo historico total de Deriv.
      </p>
    </Card>
  );
}

function OpenPositionsPanel({ positions }: { positions: OpenPos[] }) {
  return (
    <Card title="Posiciones abiertas del bot maestro" accent={BLUE}>
      {positions.length === 0 ? (
        <p style={{ color: MUTE }}>Sin contratos abiertos.</p>
      ) : (
        <Table headers={["Simbolo", "Stake", "PnL", "Abierto"]}>
          {positions.map((p, i) => (
            <tr key={`${p.contractId ?? i}`}>
              <Td>{p.symbol}</Td>
              <Td mono>{fmtUSD(p.stake)}</Td>
              <Td mono color={p.currentPnl >= 0 ? GREEN : RED}>{fmtUSD(p.currentPnl, true)}</Td>
              <Td mono>{p.openedAt ? new Date(p.openedAt).toLocaleTimeString("es-ES") : "—"}</Td>
            </tr>
          ))}
        </Table>
      )}
    </Card>
  );
}

function BotMetricsPanel({ metrics, windowSel, onChangeWindow }: {
  metrics: BotMetrics | null;
  windowSel: "24h" | "7d" | "30d";
  onChangeWindow: (w: "24h" | "7d" | "30d") => void;
}) {
  return (
    <Card
      title="Metricas del bot"
      accent={BLUE}
      action={
        <div style={{ display: "flex", gap: 6 }}>
          {(["24h", "7d", "30d"] as const).map((w) => (
            <button key={w} onClick={() => onChangeWindow(w)} style={{
              background: w === windowSel ? BLUE : "transparent",
              color: w === windowSel ? "#000" : TEXT,
              border: `1px solid ${BORD}`,
              borderRadius: 8,
              padding: "5px 12px",
              fontSize: 11,
              cursor: "pointer",
              fontWeight: 700,
            }}>{w}</button>
          ))}
        </div>
      }
    >
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12 }}>
        <MiniKpi label="PnL" value={fmtUSD(metrics?.pnl ?? 0, true)} color={(metrics?.pnl ?? 0) >= 0 ? GREEN : RED} />
        <MiniKpi label="Trades" value={String(metrics?.trades ?? 0)} />
        <MiniKpi label="WR" value={`${(metrics?.wr ?? 0).toFixed(0)}%`} />
        <MiniKpi label="PF" value={metrics?.pf == null ? "—" : metrics.pf.toFixed(3)} />
        <MiniKpi label="SL hits" value={String(metrics?.slHits ?? 0)} color={RED} />
        <MiniKpi label="Timeout wins" value={String(metrics?.timeoutWins ?? 0)} color={GREEN} />
        <MiniKpi label="Ratchet" value={String(metrics?.ratchet ?? 0)} />
      </div>
    </Card>
  );
}

function SymbolMetricsPanel({ data }: { data: SymMetrics | null }) {
  const rows = data?.symbols ?? [];
  return (
    <Card title="Metricas por simbolo" accent={BLUE}>
      {rows.length === 0 ? (
        <p style={{ color: MUTE }}>Sin datos en la ventana seleccionada.</p>
      ) : (
        <Table headers={["Simbolo", "Trades", "WR%", "PnL", "SL%"]}>
          {rows.map((r) => (
            <tr key={r.symbol}>
              <Td>{r.symbol}</Td>
              <Td mono>{r.trades}</Td>
              <Td mono>{r.wr.toFixed(0)}%</Td>
              <Td mono color={r.pnl >= 0 ? GREEN : RED}>{fmtUSD(r.pnl, true)}</Td>
              <Td mono color={r.slPct > 40 ? RED : TEXT}>
                {r.slPct.toFixed(0)}%{r.slPct > 40 ? " ⚠" : ""}
              </Td>
            </tr>
          ))}
        </Table>
      )}
    </Card>
  );
}

function AddClientModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [form, setForm] = useState({
    nombre: "",
    email: "",
    capitalInicial: "100",
    fechaInicio: new Date().toISOString().slice(0, 16),
    derivToken: "",
    derivAccountId: "",
  });
  const [err, setErr] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();

  function submit() {
    setErr(null);
    startTransition(async () => {
      try {
        const r = await fetch("/api/admin/clients", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            ...form,
            capitalInicial: Number(form.capitalInicial),
          }),
        });
        const j = await r.json();
        if (!r.ok || !j.ok) {
          setErr(j.error ?? "Error desconocido");
          return;
        }
        onCreated();
      } catch (e) {
        setErr((e as Error).message);
      }
    });
  }

  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)",
      display: "flex", alignItems: "center", justifyContent: "center", zIndex: 50,
    }}>
      <div style={{ background: "#0a1018", border: `1px solid ${BORD}`, borderRadius: 14, padding: 24, width: 420 }}>
        <h2 style={{ margin: "0 0 16px", fontSize: 16, color: TEXT }}>Anadir Cliente</h2>
        <Input label="Nombre" value={form.nombre} onChange={(v) => setForm({ ...form, nombre: v })} />
        <Input label="Email" type="email" value={form.email} onChange={(v) => setForm({ ...form, email: v })} />
        <Input label="Capital inicial (USDT)" type="number" value={form.capitalInicial} onChange={(v) => setForm({ ...form, capitalInicial: v })} />
        <Input label="Fecha de inicio" type="datetime-local" value={form.fechaInicio} onChange={(v) => setForm({ ...form, fechaInicio: v })} />
        <Input label="Token Deriv" value={form.derivToken} onChange={(v) => setForm({ ...form, derivToken: v })} />
        <Input label="Account ID Deriv" value={form.derivAccountId} onChange={(v) => setForm({ ...form, derivAccountId: v })} placeholder="CR1234567" />
        {err && <p style={{ color: RED, fontSize: 12 }}>{err}</p>}
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 16 }}>
          <button onClick={onClose} style={{ background: "transparent", color: MUTE, border: `1px solid ${BORD}`, padding: "8px 14px", borderRadius: 8, cursor: "pointer" }}>Cancelar</button>
          <button onClick={submit} disabled={isPending} style={{ background: GREEN, color: "#000", border: "none", padding: "8px 14px", borderRadius: 8, cursor: "pointer", fontWeight: 700 }}>
            {isPending ? "Creando…" : "Crear"}
          </button>
        </div>
      </div>
    </div>
  );
}

function Input({ label, value, onChange, type = "text", placeholder }: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  type?: string;
  placeholder?: string;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 10 }}>
      <label style={{ color: MUTE, fontSize: 11 }}>{label}</label>
      <input
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        style={{
          background: "#0e1520",
          border: `1px solid ${BORD}`,
          borderRadius: 8,
          color: TEXT,
          padding: "8px 10px",
          fontSize: 13,
        }}
      />
    </div>
  );
}

// ── Building blocks ─────────────────────────────────────────────────────────

function Card({ title, children, accent, action }: { title: string; children: React.ReactNode; accent?: string; action?: React.ReactNode }) {
  return (
    <section style={{
      background: CARD,
      border: `1px solid ${BORD}`,
      borderLeft: accent ? `2px solid ${accent}` : `1px solid ${BORD}`,
      borderRadius: 16,
      padding: 20,
      marginBottom: 16,
      backdropFilter: "blur(10px)",
      WebkitBackdropFilter: "blur(10px)",
    }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
        <p style={{ color: MUTE, fontSize: 10, fontWeight: 700, letterSpacing: "0.16em", textTransform: "uppercase", margin: 0 }}>{title}</p>
        {action}
      </div>
      {children}
    </section>
  );
}

function MiniKpi({ label, value, color = TEXT }: { label: string; value: string; color?: string }) {
  return (
    <div style={{ background: "rgba(255,255,255,0.02)", border: `1px solid ${BORD}`, borderRadius: 10, padding: "8px 12px" }}>
      <p style={{ margin: 0, color: MUTE, fontSize: 10, letterSpacing: "0.08em", textTransform: "uppercase" }}>{label}</p>
      <p style={{ margin: "4px 0 0", color, fontSize: 18, fontWeight: 700, fontFamily: "ui-monospace, Menlo, monospace" }}>{value}</p>
    </div>
  );
}

function Badge({ color, text }: { color: string; text: string }) {
  return (
    <span style={{
      padding: "5px 12px",
      background: `${color}1f`,
      border: `1px solid ${color}55`,
      color,
      borderRadius: 20,
      fontSize: 11,
      fontWeight: 700,
      letterSpacing: "0.1em",
    }}>{text}</span>
  );
}

function Table({ headers, children }: { headers: string[]; children: React.ReactNode }) {
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
      <thead>
        <tr>{headers.map((h) => (
          <th key={h} style={{ textAlign: "left", color: MUTE, fontSize: 10, letterSpacing: "0.1em", textTransform: "uppercase", padding: "8px 6px", borderBottom: `1px solid ${BORD}` }}>{h}</th>
        ))}</tr>
      </thead>
      <tbody>{children}</tbody>
    </table>
  );
}

function Td({ children, color = TEXT, mono }: { children: React.ReactNode; color?: string; mono?: boolean }) {
  return (
    <td style={{
      padding: "10px 6px",
      borderBottom: `1px solid ${BORD}55`,
      color,
      fontFamily: mono ? "ui-monospace, Menlo, monospace" : undefined,
    }}>{children}</td>
  );
}
