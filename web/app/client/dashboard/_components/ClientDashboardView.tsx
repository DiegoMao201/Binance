"use client";
/**
 * ClientDashboardView — Componente cliente que orquesta polling y
 * renderizado de las 4 secciones del dashboard del cliente.
 *
 * Polling:
 *  - balance + posiciones abiertas: 5s
 *  - stats: 30s
 *
 * Toda la logica de comision queda DERIVADA del balance real-time y
 * el capital_inicial; nunca se calcula a partir de datos legacy.
 */

import { useEffect, useMemo, useState } from "react";

const BG    = "#050b12";
const CARD  = "rgba(8,15,25,0.78)";
const BORD  = "rgba(76,136,170,0.24)";
const TEXT  = "#e6f2ff";
const MUTE  = "#8aa5bf";
const GREEN = "#19c37d";
const RED   = "#ff6b6b";
const AMBER = "#ffbf47";
const BLUE  = "#3dd6ff";

type AccountState = {
  ok: boolean;
  profile?: {
    id: string;
    displayName: string;
    email: string;
    derivAccountId: string | null;
    fechaInicio: string | null;
  };
  estado?: {
    capitalInicial: number;
    balanceActual: number;
    gananciaNeta: number;
    rendimientoPct: number;
    enModoRecuperacion: boolean;
    mensajeEstado: string;
    comisionTotalCobrada: number;
    parteClienteSobreUmbral: number;
    comisionEstimadaSobreUmbral: number;
  };
  balanceMeta?: {
    source: string;
    currency?: string;
    loginid?: string;
    cachedAt?: string | null;
    derivError?: string;
  };
  error?: string;
};

type OpenPositions = {
  ok: boolean;
  positions?: Array<{
    symbol: string;
    stake: number;
    currentPnl: number;
    status: string;
    openedAt: string | null;
  }>;
};

type StatsState = {
  ok: boolean;
  activeSince?: string;
  diasActivo?: number;
  totalTrades?: number;
  winRate?: number;
  wins?: number;
  losses?: number;
  bestDay?: number;
  positiveDays?: number;
  totalDays?: number;
  promedioDiario?: number;
  totalPnl?: number;
  dailyPnl?: Array<{ date: string; pnl: number }>;
  latestDayKey?: string | null;
  latestDayPnl?: number;
  todayKeyUtc?: string;
  todayPnlUtc?: number;
  estimatedServiceLatestDay?: number;
  estimatedServiceTodayUtc?: number;
  estimatedServiceTotal?: number;
  estimatedClientShareLatestDay?: number;
  estimatedClientShareTotal?: number;
};

function fmtUSD(n: number, sign = false): string {
  const prefix = n < 0 ? "-" : sign && n > 0 ? "+" : "";
  return `${prefix}$${Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtPct(n: number, sign = true): string {
  const prefix = n < 0 ? "" : sign ? "+" : "";
  return `${prefix}${n.toFixed(2)}%`;
}

function fmtElapsed(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 0 || Number.isNaN(ms)) return "—";
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const sec = s % 60;
  if (m > 60) return `${Math.floor(m / 60)}h ${m % 60}m`;
  return `${m}m ${sec.toString().padStart(2, "0")}s`;
}

export function ClientDashboardView() {
  const [account, setAccount] = useState<AccountState | null>(null);
  const [positions, setPositions] = useState<OpenPositions | null>(null);
  const [stats, setStats] = useState<StatsState | null>(null);
  const [tick, setTick] = useState(0);

  // Polling cada 5s para balance + posiciones
  useEffect(() => {
    let cancelled = false;
    async function pull() {
      try {
        const [a, p] = await Promise.all([
          fetch("/api/client/account-state", { cache: "no-store" }).then((r) => r.json()),
          fetch("/api/client/open-positions", { cache: "no-store" }).then((r) => r.json()),
        ]);
        if (!cancelled) {
          setAccount(a);
          setPositions(p);
        }
      } catch {
        // keep last
      }
    }
    pull();
    const id = setInterval(pull, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // Polling cada 30s para stats
  useEffect(() => {
    let cancelled = false;
    async function pull() {
      try {
        const s = await fetch("/api/client/stats", { cache: "no-store" }).then((r) => r.json());
        if (!cancelled) setStats(s);
      } catch { /* noop */ }
    }
    pull();
    const id = setInterval(pull, 30_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // tick para refrescar "Tiempo" de posiciones cada 1s
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const estado = account?.estado;
  const enRec = Boolean(estado?.enModoRecuperacion);

  return (
    <div style={{
      minHeight: "100vh",
      background: [
        "radial-gradient(900px 600px at 8% -8%, rgba(25,195,125,0.10), transparent 62%)",
        "radial-gradient(760px 460px at 95% 10%, rgba(61,214,255,0.10), transparent 65%)",
        "linear-gradient(180deg, #060d17 0%, #050b12 100%)",
      ].join(", "),
      color: TEXT,
      fontFamily: "Sora, Manrope, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
      padding: "24px",
      boxSizing: "border-box",
    }}>
      <Header account={account} tick={tick} />

      {account?.ok === false && (
        <Banner color={RED}>{account.error ?? "Error cargando cuenta."}</Banner>
      )}

      {enRec && estado && (
        <Banner color={AMBER}>
          <strong>⚠ En recuperacion</strong> — faltan {fmtUSD(estado.capitalInicial - estado.balanceActual)} para recuperar tu capital inicial.
          Sin comisiones hasta volver a {fmtUSD(estado.capitalInicial)}.
        </Banner>
      )}

      {/* Seccion 1 — Resumen */}
      <AccountSummary state={account} stats={stats} />

      {/* Seccion 2 — Posiciones abiertas */}
      <OpenPositionsTable positions={positions?.positions ?? []} tick={tick} />

      {/* Seccion 3 — Estadisticas */}
      <ClientStats stats={stats} />

      {/* Seccion 4 — Grafico diario */}
      <DailyPnlChart points={stats?.dailyPnl ?? []} />
    </div>
  );
}

function Header({ account, tick }: { account: AccountState | null; tick: number }) {
  void tick;
  const meta = account?.balanceMeta;
  const live = meta?.source === "deriv_ws";
  return (
    <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
      <div>
        <h1 style={{ fontSize: 22, fontWeight: 800, margin: 0, letterSpacing: "-0.02em" }}>
          ◈ OptiFerre <span style={{ color: GREEN }}>Cliente</span>
        </h1>
        <p style={{ color: MUTE, fontSize: 11, margin: "4px 0 0", letterSpacing: "0.08em" }}>
          {account?.profile?.displayName ?? account?.profile?.email ?? "—"}
          {account?.profile?.derivAccountId ? ` · Deriv ${account.profile.derivAccountId}` : ""}
        </p>
      </div>
      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        <span style={{
          padding: "5px 12px",
          background: live ? `${GREEN}1f` : `${MUTE}1f`,
          border: `1px solid ${live ? GREEN : MUTE}55`,
          color: live ? GREEN : MUTE,
          borderRadius: 20,
          fontSize: 11,
          fontWeight: 700,
          letterSpacing: "0.1em",
        }}>
          {live ? "● BALANCE LIVE" : "○ BALANCE CACHE"}
        </span>
      </div>
    </header>
  );
}

function Banner({ color, children }: { color: string; children: React.ReactNode }) {
  return (
    <div style={{
      background: `${color}14`,
      border: `1px solid ${color}55`,
      color,
      padding: "12px 16px",
      borderRadius: 12,
      marginBottom: 16,
      fontSize: 13,
    }}>{children}</div>
  );
}

function AccountSummary({ state, stats }: { state: AccountState | null; stats: StatsState | null }) {
  const e = state?.estado;
  if (!e) return (
    <Card title="Resumen de cuenta"><p style={{ color: MUTE }}>Cargando…</p></Card>
  );

  const enRec = e.enModoRecuperacion;
  const countingFromIso = state?.profile?.fechaInicio ?? stats?.activeSince ?? null;
  const countingFromHuman = countingFromIso
    ? new Date(countingFromIso).toLocaleString("es-ES", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    })
    : "—";
  const statsOk = Boolean(stats?.ok);
  const latestDayKey = statsOk ? (stats?.latestDayKey ?? null) : null;
  const latestDayPnl = statsOk ? (stats?.latestDayPnl ?? 0) : 0;
  const latestDayService = statsOk ? (stats?.estimatedServiceLatestDay ?? Math.max(latestDayPnl, 0) * 0.20) : 0;
  const latestDayClientShare = statsOk ? (stats?.estimatedClientShareLatestDay ?? (latestDayPnl - latestDayService)) : 0;
  const totalPnlReal = statsOk ? (stats?.totalPnl ?? 0) : 0;
  const totalServiceEstimated = statsOk ? (stats?.estimatedServiceTotal ?? Math.max(totalPnlReal, 0) * 0.20) : 0;

  return (
    <Card title="Resumen de cuenta" accent={enRec ? AMBER : GREEN}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 14 }}>
        <Kpi label="Capital Inicial" value={fmtUSD(e.capitalInicial)} accent={BLUE} />
        <Kpi label="Balance Actual" value={fmtUSD(e.balanceActual)} accent={BLUE} mono />
        <Kpi label="PnL Real Desde Inicio" value={fmtUSD(totalPnlReal, true)} accent={totalPnlReal >= 0 ? GREEN : RED} />
        <Kpi
          label="Ultimo Dia Cerrado"
          value={latestDayKey ? new Date(`${latestDayKey}T00:00:00Z`).toLocaleDateString("es-ES") : "—"}
          sub={latestDayKey ? "Basado en cierres reales" : "Sin cierres todavia"}
        />
        <Kpi label="PnL Ultimo Cierre" value={fmtUSD(latestDayPnl, true)} accent={latestDayPnl >= 0 ? GREEN : RED} />
        <Kpi label="Tu Parte Ultimo Cierre (80%)" value={fmtUSD(latestDayClientShare, true)} accent={latestDayClientShare >= 0 ? GREEN : RED} />
        <Kpi label="Servicio Ultimo Cierre (20%)" value={fmtUSD(enRec ? 0 : latestDayService)} accent={enRec ? MUTE : AMBER} />
        <Kpi label="Servicio Acumulado Estimado" value={fmtUSD(enRec ? 0 : totalServiceEstimated)} accent={enRec ? MUTE : AMBER} />
      </div>
      <p style={{ marginTop: 14, color: enRec ? AMBER : GREEN, fontSize: 13 }}>
        Estado: <strong>● {e.mensajeEstado}</strong>
      </p>
      <div style={{
        marginTop: 10,
        background: "rgba(255,255,255,0.02)",
        border: `1px solid ${BORD}`,
        borderRadius: 10,
        padding: "10px 12px",
        color: MUTE,
        fontSize: 12,
        lineHeight: 1.5,
      }}>
        Inicio de conteo cobrable: <strong style={{ color: TEXT }}>{countingFromHuman}</strong>.
        Liquidacion transparente: el servicio se estima sobre PnL real cerrado, no sobre saldo historico total.
        Formula visible: <strong style={{ color: TEXT }}>Servicio = 20% × max(PnL del dia cerrado, 0)</strong>.
        {enRec && " En recuperacion activa, por lo tanto el servicio mostrado se pausa en 0."}
      </div>
    </Card>
  );
}

function OpenPositionsTable({ positions, tick }: { positions: OpenPositions["positions"]; tick: number }) {
  void tick;
  return (
    <Card title="Posiciones abiertas" accent={BLUE}>
      {positions && positions.length > 0 ? (
        <Table headers={["Simbolo", "Entrada", "PnL", "Estado", "Tiempo"]}>
          {positions.map((p, i) => (
            <tr key={`${p.symbol}-${i}`}>
              <Td>{p.symbol}</Td>
              <Td mono>{fmtUSD(p.stake)}</Td>
              <Td mono color={p.currentPnl >= 0 ? GREEN : RED}>{fmtUSD(p.currentPnl, true)}</Td>
              <Td color={GREEN}>{p.status}</Td>
              <Td mono>{fmtElapsed(p.openedAt)}</Td>
            </tr>
          ))}
        </Table>
      ) : (
        <p style={{ color: MUTE, fontSize: 13 }}>Sin posiciones abiertas ahora.</p>
      )}
    </Card>
  );
}

function ClientStats({ stats }: { stats: StatsState | null }) {
  if (!stats || !stats.ok) {
    return <Card title="Estadisticas desde inicio de cuenta"><p style={{ color: MUTE }}>Cargando…</p></Card>;
  }
  return (
    <Card title="Estadisticas desde inicio de cuenta" accent={BLUE}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 14 }}>
        <Kpi label="Activo desde" value={stats.activeSince ? new Date(stats.activeSince).toLocaleDateString("es-ES", { year: "numeric", month: "short", day: "2-digit" }) : "—"} sub={`${stats.diasActivo ?? 0} dias`} />
        <Kpi label="Total trades" value={String(stats.totalTrades ?? 0)} />
        <Kpi label="Win Rate" value={`${(stats.winRate ?? 0).toFixed(0)}%`} accent={(stats.winRate ?? 0) >= 50 ? GREEN : RED} />
        <Kpi label="Mejor dia" value={fmtUSD(stats.bestDay ?? 0, true)} accent={GREEN} />
        <Kpi label="Dias positivos" value={`${stats.positiveDays ?? 0} de ${stats.totalDays ?? 0}`} />
        <Kpi label="Promedio diario" value={fmtUSD(stats.promedioDiario ?? 0, true)} accent={(stats.promedioDiario ?? 0) >= 0 ? GREEN : RED} />
        <Kpi label="Cobro Ultimo Cierre" value={fmtUSD(stats.estimatedServiceLatestDay ?? 0)} accent={AMBER} />
        <Kpi label="Cobro Acumulado Estimado" value={fmtUSD(stats.estimatedServiceTotal ?? 0)} accent={AMBER} />
      </div>
    </Card>
  );
}

function DailyPnlChart({ points }: { points: Array<{ date: string; pnl: number }> }) {
  const latest = useMemo(
    () => [...points].sort((a, b) => (a.date < b.date ? 1 : -1)).slice(0, 10),
    [points],
  );
  const neto = useMemo(
    () => points.reduce((acc, p) => acc + p.pnl, 0),
    [points],
  );
  return (
    <Card title="PnL por dia (desde inicio)" accent={BLUE}>
      {points.length === 0 ? (
        <p style={{ color: MUTE, fontSize: 13 }}>Sin datos todavia.</p>
      ) : (
        <div>
          <div style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: 10,
            fontSize: 12,
            color: MUTE,
          }}>
            <span>Ultimos {latest.length} dias</span>
            <span>
              Neto acumulado: <strong style={{ color: neto >= 0 ? GREEN : RED }}>{fmtUSD(neto, true)}</strong>
            </span>
          </div>

          <div style={{ display: "grid", gap: 8 }}>
            {latest.map((p) => (
              <div key={p.date} style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                border: `1px solid ${BORD}`,
                background: "rgba(255,255,255,0.02)",
                borderRadius: 10,
                padding: "8px 10px",
              }}>
                <span style={{ color: TEXT, fontSize: 12 }}>{new Date(`${p.date}T00:00:00Z`).toLocaleDateString("es-ES")}</span>
                <span style={{ color: p.pnl >= 0 ? GREEN : RED, fontSize: 13, fontFamily: "ui-monospace, Menlo, monospace" }}>
                  {fmtUSD(p.pnl, true)}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

// ── Building blocks ─────────────────────────────────────────────────────────

function Card({ title, children, accent }: { title: string; children: React.ReactNode; accent?: string }) {
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
      <p style={{
        color: MUTE,
        fontSize: 10,
        fontWeight: 700,
        letterSpacing: "0.16em",
        textTransform: "uppercase",
        margin: "0 0 14px",
      }}>{title}</p>
      {children}
    </section>
  );
}

function Kpi({ label, value, accent = TEXT, sub, mono }: { label: string; value: string; accent?: string; sub?: string; mono?: boolean }) {
  return (
    <div style={{ background: "rgba(255,255,255,0.02)", border: `1px solid ${BORD}`, borderRadius: 10, padding: "10px 12px" }}>
      <p style={{ margin: 0, color: MUTE, fontSize: 10, letterSpacing: "0.1em", textTransform: "uppercase" }}>{label}</p>
      <p style={{ margin: "6px 0 2px", color: accent, fontSize: 18, fontWeight: 700, fontFamily: mono ? "ui-monospace, Menlo, monospace" : undefined }}>{value}</p>
      {sub && <p style={{ margin: 0, color: MUTE, fontSize: 11 }}>{sub}</p>}
    </div>
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
