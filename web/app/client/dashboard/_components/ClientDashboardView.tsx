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

import { useEffect, useState } from "react";

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
  operationalCapitalToday?: number;
  serviceDueTodayUtc?: number;
  clientNetTodayUtc?: number;
  projectedNextDayCapital?: number;
  yesterdayKeyUtc?: string;
  yesterdayPnlUtc?: number;
  serviceDueYesterdayUtc?: number;
  clientNetYesterdayUtc?: number;
  operationalCapitalYesterday?: number;
  projectedAfterYesterdayCapital?: number;
  tradesYesterdayUtc?: number;
  firstDayPartial?: boolean;
  lastSettledDayKey?: string | null;
  lastSettledDayPnl?: number;
  lastSettledDayService?: number;
  dailySettlement?: Array<{
    dayKey: string;
    pnl: number;
    service: number;
    clientNet: number;
    capitalStart: number;
    capitalEnd: number;
    trades: number;
    partialDay: boolean;
  }>;
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
  const [selectedDayKey, setSelectedDayKey] = useState("");
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

  useEffect(() => {
    if (!stats?.ok) return;
    const settlements = Array.isArray(stats.dailySettlement) ? stats.dailySettlement : [];
    const days = settlements.map((d) => d.dayKey);
    if (!days.length) return;
    const fallback = stats.yesterdayKeyUtc ?? stats.todayKeyUtc ?? days[days.length - 1];
    const next = selectedDayKey && days.includes(selectedDayKey)
      ? selectedDayKey
      : (days.includes(fallback ?? "") ? (fallback ?? "") : days[days.length - 1]);
    if (next && next !== selectedDayKey) {
      setSelectedDayKey(next);
    }
  }, [stats, selectedDayKey]);

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
      <DailyBiPanel stats={stats} selectedDayKey={selectedDayKey} onChangeDay={setSelectedDayKey} />
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
  const todayPnlUtc = statsOk ? (stats?.todayPnlUtc ?? 0) : 0;
  const serviceDueToday = statsOk
    ? (stats?.serviceDueTodayUtc ?? stats?.estimatedServiceTodayUtc ?? Math.max(todayPnlUtc, 0) * 0.20)
    : 0;
  const clientNetToday = statsOk ? (stats?.clientNetTodayUtc ?? (todayPnlUtc - serviceDueToday)) : 0;
  const yesterdayPnlUtc = statsOk ? (stats?.yesterdayPnlUtc ?? 0) : 0;
  const serviceDueYesterday = statsOk
    ? (stats?.serviceDueYesterdayUtc ?? Math.max(yesterdayPnlUtc, 0) * 0.20)
    : 0;
  const clientNetYesterday = statsOk ? (stats?.clientNetYesterdayUtc ?? (yesterdayPnlUtc - serviceDueYesterday)) : 0;
  const operationalCapitalToday = statsOk ? (stats?.operationalCapitalToday ?? e.capitalInicial) : e.capitalInicial;
  const projectedNextDayCapital = statsOk
    ? (stats?.projectedNextDayCapital ?? (operationalCapitalToday + todayPnlUtc))
    : e.capitalInicial;
  const firstDayPartial = statsOk ? Boolean(stats?.firstDayPartial) : false;
  const lastSettledDayKey = statsOk ? (stats?.lastSettledDayKey ?? null) : null;
  const lastSettledDayPnl = statsOk ? (stats?.lastSettledDayPnl ?? 0) : 0;
  const lastSettledDayService = statsOk
    ? (stats?.lastSettledDayService ?? Math.max(lastSettledDayPnl, 0) * 0.20)
    : 0;
  const totalPnlReal = statsOk ? (stats?.totalPnl ?? 0) : 0;
  const totalServiceEstimated = statsOk ? (stats?.estimatedServiceTotal ?? Math.max(totalPnlReal, 0) * 0.20) : 0;

  return (
    <Card title="Resumen de cuenta" accent={enRec ? AMBER : GREEN}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 14 }}>
        <Kpi label="Capital Inicial" value={fmtUSD(e.capitalInicial)} accent={BLUE} />
        <Kpi label="Capital Operativo Hoy" value={fmtUSD(operationalCapitalToday)} accent={BLUE} mono />
        <Kpi label="Balance Actual" value={fmtUSD(e.balanceActual)} accent={BLUE} mono />
        <Kpi label="PnL Real Desde Inicio" value={fmtUSD(totalPnlReal, true)} accent={totalPnlReal >= 0 ? GREEN : RED} />
        <Kpi label="PnL Hoy (UTC)" value={fmtUSD(todayPnlUtc, true)} accent={todayPnlUtc >= 0 ? GREEN : RED} />
        <Kpi label="Servicio Hoy (20%)" value={fmtUSD(enRec ? 0 : serviceDueToday)} accent={enRec ? MUTE : AMBER} />
        <Kpi label="Tu Neto Hoy" value={fmtUSD(clientNetToday, true)} accent={clientNetToday >= 0 ? GREEN : RED} />
        <Kpi label="PnL Ayer (UTC)" value={fmtUSD(yesterdayPnlUtc, true)} accent={yesterdayPnlUtc >= 0 ? GREEN : RED} />
        <Kpi label="Servicio Ayer (20%)" value={fmtUSD(enRec ? 0 : serviceDueYesterday)} accent={enRec ? MUTE : AMBER} />
        <Kpi label="Tu Neto Ayer" value={fmtUSD(clientNetYesterday, true)} accent={clientNetYesterday >= 0 ? GREEN : RED} />
        <Kpi label="Base Capital Manana" value={fmtUSD(projectedNextDayCapital)} accent={BLUE} mono />
        <Kpi
          label="Ultimo Dia Liquidado"
          value={lastSettledDayKey ? new Date(`${lastSettledDayKey}T00:00:00Z`).toLocaleDateString("es-ES") : "—"}
          sub={lastSettledDayKey ? "Dia completo cerrado" : "Sin dia liquidado aun"}
        />
        <Kpi label="PnL Ultimo Liquidado" value={fmtUSD(lastSettledDayPnl, true)} accent={lastSettledDayPnl >= 0 ? GREEN : RED} />
        <Kpi label="Servicio Ultimo Liquidado" value={fmtUSD(enRec ? 0 : lastSettledDayService)} accent={enRec ? MUTE : AMBER} />
        <Kpi
          label="Cobro Acumulado Referencial"
          value={fmtUSD(enRec ? 0 : totalServiceEstimated)}
          sub="Solo para contexto historico"
        />
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
        {firstDayPartial ? " Primer dia parcial: se cuenta solo desde tu hora exacta de alta." : ""}
        Liquidacion transparente diaria: se cobra solo el PnL positivo del dia UTC en curso.
        Formula visible: <strong style={{ color: TEXT }}>Servicio hoy = 20% × max(PnL hoy UTC, 0)</strong>.
        Base del siguiente dia: <strong style={{ color: TEXT }}>Capital manana = capital operativo hoy + PnL hoy</strong>.
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
        <Kpi label="Cobro Hoy (UTC)" value={fmtUSD(stats.serviceDueTodayUtc ?? stats.estimatedServiceTodayUtc ?? 0)} accent={AMBER} />
        <Kpi label="Capital Base Manana" value={fmtUSD(stats.projectedNextDayCapital ?? 0)} accent={BLUE} />
      </div>
    </Card>
  );
}

function DailyBiPanel({
  stats,
  selectedDayKey,
  onChangeDay,
}: {
  stats: StatsState | null;
  selectedDayKey: string;
  onChangeDay: (value: string) => void;
}) {
  if (!stats || !stats.ok) {
    return <Card title="Centro de control diario"><p style={{ color: MUTE }}>Cargando…</p></Card>;
  }

  const settlements = [...(stats.dailySettlement ?? [])].sort((a, b) => (a.dayKey < b.dayKey ? -1 : 1));
  if (!settlements.length) {
    return (
      <Card title="Centro de control diario" accent={BLUE}>
        <p style={{ color: MUTE, fontSize: 13 }}>Aun no hay cierres diarios para construir la vista BI.</p>
      </Card>
    );
  }

  const selected = settlements.find((d) => d.dayKey === selectedDayKey) ?? settlements[settlements.length - 1];
  const totalService = settlements.reduce((acc, d) => acc + d.service, 0);
  const totalNet = settlements.reduce((acc, d) => acc + d.clientNet, 0);
  const recent = settlements.slice(-21);
  const minDay = settlements[0].dayKey;
  const maxDay = settlements[settlements.length - 1].dayKey;
  const todayKey = stats.todayKeyUtc ?? "";
  const yesterdayKey = stats.yesterdayKeyUtc ?? "";

  return (
    <Card title="Centro de control diario" accent={BLUE}>
      <div style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 10,
        marginBottom: 12,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ color: MUTE, fontSize: 12 }}>Filtro por fecha:</span>
          <input
            type="date"
            min={minDay}
            max={maxDay}
            value={selected.dayKey}
            onChange={(e) => onChangeDay(e.target.value)}
            style={{
              background: "rgba(8,15,25,0.95)",
              border: `1px solid ${BORD}`,
              color: TEXT,
              borderRadius: 8,
              padding: "6px 10px",
              fontSize: 12,
            }}
          />
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <button onClick={() => todayKey && onChangeDay(todayKey)} style={chipButtonStyle(selected.dayKey === todayKey)}>Hoy</button>
          <button onClick={() => yesterdayKey && onChangeDay(yesterdayKey)} style={chipButtonStyle(selected.dayKey === yesterdayKey)}>Ayer</button>
          <button onClick={() => onChangeDay(maxDay)} style={chipButtonStyle(selected.dayKey === maxDay)}>Ultimo cierre</button>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 10, marginBottom: 14 }}>
        <Kpi label="Dia seleccionado" value={new Date(`${selected.dayKey}T00:00:00Z`).toLocaleDateString("es-ES")} sub={selected.partialDay ? "dia parcial" : "dia completo"} />
        <Kpi label="PnL del dia" value={fmtUSD(selected.pnl, true)} accent={selected.pnl >= 0 ? GREEN : RED} />
        <Kpi label="Servicio del dia (20%)" value={fmtUSD(selected.service)} accent={AMBER} />
        <Kpi label="Neto cliente del dia" value={fmtUSD(selected.clientNet, true)} accent={selected.clientNet >= 0 ? GREEN : RED} />
        <Kpi label="Capital inicio dia" value={fmtUSD(selected.capitalStart)} accent={BLUE} mono />
        <Kpi label="Capital cierre dia" value={fmtUSD(selected.capitalEnd)} accent={BLUE} mono />
        <Kpi label="Trades del dia" value={String(selected.trades)} />
        <Kpi label="Servicio acumulado" value={fmtUSD(totalService)} accent={AMBER} />
      </div>

      <div style={{
        display: "grid",
        gridTemplateColumns: "1.35fr 1fr",
        gap: 12,
      }}>
        <GrowthCurveCard points={recent.map((d) => ({ dayKey: d.dayKey, value: d.capitalEnd }))} />
        <div style={{
          border: `1px solid ${BORD}`,
          borderRadius: 12,
          padding: 12,
          background: "linear-gradient(180deg, rgba(13,22,34,0.95) 0%, rgba(7,12,20,0.95) 100%)",
        }}>
          <p style={{ margin: "0 0 8px", color: MUTE, fontSize: 11, letterSpacing: "0.1em", textTransform: "uppercase" }}>
            Resumen historico
          </p>
          <div style={{ display: "grid", gap: 8 }}>
            <MiniStatLine label="Dias con cierres" value={String(settlements.length)} color={TEXT} />
            <MiniStatLine label="Neto acumulado cliente" value={fmtUSD(totalNet, true)} color={totalNet >= 0 ? GREEN : RED} />
            <MiniStatLine label="Mejor dia" value={fmtUSD(Math.max(...settlements.map((d) => d.pnl)), true)} color={GREEN} />
            <MiniStatLine label="Peor dia" value={fmtUSD(Math.min(...settlements.map((d) => d.pnl)), true)} color={RED} />
          </div>
        </div>
      </div>
    </Card>
  );
}

function GrowthCurveCard({ points }: { points: Array<{ dayKey: string; value: number }> }) {
  if (points.length < 2) {
    return (
      <div style={{ border: `1px solid ${BORD}`, borderRadius: 12, padding: 12, background: "rgba(255,255,255,0.02)" }}>
        <p style={{ margin: 0, color: MUTE, fontSize: 12 }}>Se requieren al menos 2 dias para dibujar la curva de crecimiento.</p>
      </div>
    );
  }

  const width = 1000;
  const height = 220;
  const values = points.map((p) => p.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = Math.max(1, max - min);
  const coords = points.map((p, i) => {
    const x = (i / (points.length - 1)) * width;
    const y = height - ((p.value - min) / range) * (height - 24) - 12;
    return { x, y };
  });
  const linePath = coords.map((c, i) => `${i === 0 ? "M" : "L"} ${c.x.toFixed(2)} ${c.y.toFixed(2)}`).join(" ");
  const areaPath = `${linePath} L ${width} ${height} L 0 ${height} Z`;

  return (
    <div style={{
      border: `1px solid ${BORD}`,
      borderRadius: 12,
      padding: 12,
      background: "linear-gradient(180deg, rgba(13,22,34,0.95) 0%, rgba(7,12,20,0.95) 100%)",
    }}>
      <p style={{ margin: "0 0 8px", color: MUTE, fontSize: 11, letterSpacing: "0.1em", textTransform: "uppercase" }}>
        Curva de crecimiento (ultimos {points.length} dias)
      </p>
      <svg viewBox={`0 0 ${width} ${height}`} style={{ width: "100%", height: 220, display: "block" }}>
        <defs>
          <linearGradient id="growthFillClient" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="rgba(61,214,255,0.38)" />
            <stop offset="100%" stopColor="rgba(61,214,255,0.02)" />
          </linearGradient>
        </defs>
        <line x1={0} y1={height - 1} x2={width} y2={height - 1} stroke="rgba(138,165,191,0.25)" strokeWidth="1" />
        <path d={areaPath} fill="url(#growthFillClient)" />
        <path d={linePath} fill="none" stroke={BLUE} strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <div style={{ display: "flex", justifyContent: "space-between", marginTop: 6, fontSize: 11, color: MUTE }}>
        <span>{new Date(`${points[0].dayKey}T00:00:00Z`).toLocaleDateString("es-ES")}</span>
        <span style={{ color: BLUE, fontFamily: "ui-monospace, Menlo, monospace" }}>{fmtUSD(points[points.length - 1].value)}</span>
      </div>
    </div>
  );
}

function chipButtonStyle(active: boolean) {
  return {
    background: active ? BLUE : "transparent",
    color: active ? "#000" : TEXT,
    border: `1px solid ${BORD}`,
    borderRadius: 8,
    padding: "6px 10px",
    fontSize: 11,
    fontWeight: 700,
    cursor: "pointer",
  };
}

function MiniStatLine({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div style={{
      display: "flex",
      justifyContent: "space-between",
      alignItems: "center",
      borderBottom: `1px solid ${BORD}55`,
      paddingBottom: 6,
      fontSize: 12,
      color: MUTE,
    }}>
      <span>{label}</span>
      <span style={{ color: color ?? TEXT, fontFamily: "ui-monospace, Menlo, monospace", fontWeight: 700 }}>{value}</span>
    </div>
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
