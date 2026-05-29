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

import { useEffect, useState, useTransition } from "react";

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
  billingWhatsApp?: string | null;
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
  todayKeyUtc: string;
  pnlBeforeTodayUtc: number;
  operationalCapitalToday: number;
  serviceDueTodayUtc: number;
  clientNetTodayUtc: number;
  projectedNextDayCapital: number;
  yesterdayKeyUtc: string;
  yesterdayPnlUtc: number;
  serviceDueYesterdayUtc: number;
  clientNetYesterdayUtc: number;
  operationalCapitalYesterday: number;
  projectedAfterYesterdayCapital: number;
  tradesYesterdayUtc: number;
  firstDayPartial: boolean;
  lastSettledDayKey: string | null;
  lastSettledDayPnl: number;
  lastSettledDayService: number;
  dailySettlement: Array<{
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

type ClientsResponse = {
  ok: boolean;
  clients?: ClientRow[];
  totalAdminFee20?: number;
  totalAdminFee20LatestDay?: number;
  totalAdminFee20Estimated?: number;
  totalRealizedPnl?: number;
  totalServiceDueTodayUtc?: number;
  totalServiceDueYesterdayUtc?: number;
  totalPnlYesterdayUtc?: number;
  totalProjectedNextDayCapital?: number;
  dailyBiTimeline?: Array<{
    dayKey: string;
    pnl: number;
    service: number;
    clientNet: number;
    capitalStart: number;
    capitalEnd: number;
    trades: number;
    clients: number;
  }>;
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

type BillingMode = "rolling_7d" | "rolling_15d" | "since_last_payment" | "custom";
type BillingStatus = "pending" | "paid" | "waived";

type BillingStatementRow = {
  id: string;
  userId: string;
  displayName: string;
  email: string;
  billingWhatsApp: string | null;
  periodStart: string;
  periodEnd: string;
  mode: BillingMode;
  tradesCount: number;
  pnlUsdt: number;
  serviceDueUsdt: number;
  clientNetUsdt: number;
  capitalStartUsdt: number;
  capitalEndUsdt: number;
  status: BillingStatus;
  paidAmountUsdt: number;
  pendingAmountUsdt: number;
  paidAt: string | null;
  paymentChannel: string | null;
  paymentReference: string | null;
  notes: string | null;
  emailSentAt: string | null;
  generatedAt: string;
  updatedAt: string;
};

type BillingListResponse = {
  ok: boolean;
  statements?: BillingStatementRow[];
  totals?: {
    pendingAmountUsdt: number;
    serviceDueUsdt: number;
    paidAmountUsdt: number;
    pendingCount: number;
    paidCount: number;
    waivedCount: number;
    clientsWithDebt: number;
  };
  paymentMethods?: {
    nequi?: string;
    daviKey?: string;
  };
  count?: number;
  error?: string;
};

function fmtUSD(n: number, sign = false): string {
  const prefix = n < 0 ? "-" : sign && n > 0 ? "+" : "";
  return `${prefix}$${Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtMaybeUSD(n: number | null, sign = false): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return fmtUSD(n, sign);
}

function addDaysUtc(dayKeyUtc: string, deltaDays: number): string {
  const d = new Date(`${dayKeyUtc}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + deltaDays);
  return d.toISOString().slice(0, 10);
}

function sanitizeWhatsapp(raw: string): string {
  const cleaned = raw.replace(/[^\d+]/g, "").trim();
  if (!cleaned) return "";
  return cleaned.startsWith("+") ? cleaned.slice(1) : cleaned;
}

function modeLabel(mode: BillingMode): string {
  if (mode === "rolling_7d") return "Ultimos 7 dias";
  if (mode === "rolling_15d") return "Ultimos 15 dias";
  if (mode === "custom") return "Rango manual";
  return "Desde ultimo pago";
}

function buildWhatsappChargeMessage(statement: BillingStatementRow, nequi: string, daviKey: string): string {
  const pnlSign = statement.pnlUsdt > 0 ? "+" : "";
  return [
    `Hola ${statement.displayName},`,
    "",
    "Te comparto tu estado de cuenta de servicio OptiFerre.",
    `Corte: ${statement.periodStart} a ${statement.periodEnd} (UTC)`,
    `Operaciones cerradas: ${statement.tradesCount}`,
    `PnL del periodo: ${pnlSign}${statement.pnlUsdt.toFixed(2)} USDT`,
    `Valor servicio (20%): ${statement.serviceDueUsdt.toFixed(2)} USDT`,
    `Saldo pendiente: ${statement.pendingAmountUsdt.toFixed(2)} USDT`,
    "",
    "Canales de pago:",
    `Nequi: ${nequi}`,
    `Llave Davivienda: ${daviKey}`,
    "",
    "Cuando realices el pago, por favor comparte el soporte para marcar tu cuenta como pagada.",
  ].join("\n");
}

export function AdminDashboardView() {
  const todayKeyUtc = new Date().toISOString().slice(0, 10);
  const [bot, setBot] = useState<BotStatus | null>(null);
  const [clients, setClients] = useState<ClientsResponse | null>(null);
  const [openPos, setOpenPos] = useState<OpenResp | null>(null);
  const [windowSel, setWindowSel] = useState<"24h" | "7d" | "30d">("24h");
  const [metrics, setMetrics] = useState<BotMetrics | null>(null);
  const [symMetrics, setSymMetrics] = useState<SymMetrics | null>(null);
  const [selectedDayKey, setSelectedDayKey] = useState("");
  const [showAdd, setShowAdd] = useState(false);
  const [adminTab, setAdminTab] = useState<"control" | "billing">("control");
  const [billing, setBilling] = useState<BillingListResponse | null>(null);
  const [billingStatusFilter, setBillingStatusFilter] = useState<"all" | BillingStatus>("all");
  const [billingClientFilter, setBillingClientFilter] = useState<string>("all");
  const [billingMode, setBillingMode] = useState<BillingMode>("since_last_payment");
  const [billingCutoffDay, setBillingCutoffDay] = useState(addDaysUtc(todayKeyUtc, -1));
  const [billingStartDay, setBillingStartDay] = useState(addDaysUtc(todayKeyUtc, -14));
  const [billingEndDay, setBillingEndDay] = useState(addDaysUtc(todayKeyUtc, -1));
  const [billingLoading, setBillingLoading] = useState(false);
  const [billingError, setBillingError] = useState<string | null>(null);
  const [billingActionBusyId, setBillingActionBusyId] = useState<string | null>(null);
  const [whatsAppDraftByClient, setWhatsAppDraftByClient] = useState<Record<string, string>>({});

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
        if (!cancelled) {
          setClients(c);
          const rows = Array.isArray(c?.clients) ? c.clients : [];
          setWhatsAppDraftByClient((prev) => {
            const next = { ...prev };
            for (const row of rows) {
              if (typeof next[row.id] !== "string") {
                next[row.id] = row.billingWhatsApp ?? "";
              }
            }
            return next;
          });
        }
      } catch { /* noop */ }
    }
    pull();
    const id = setInterval(pull, 15_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function pullBilling() {
      try {
        const params = new URLSearchParams();
        if (billingStatusFilter !== "all") params.set("status", billingStatusFilter);
        if (billingClientFilter !== "all") params.set("userId", billingClientFilter);
        params.set("limit", "700");
        const res = await fetch(`/api/admin/billing/statements?${params.toString()}`, { cache: "no-store" });
        const payload = await res.json();
        if (!cancelled) {
          if (!res.ok || !payload?.ok) {
            setBillingError(payload?.error ?? "No se pudo cargar cobranzas.");
          } else {
            setBillingError(null);
            setBilling(payload);
          }
        }
      } catch {
        if (!cancelled) setBillingError("No se pudo cargar cobranzas.");
      }
    }
    pullBilling();
    const id = setInterval(pullBilling, 30_000);
    return () => { cancelled = true; clearInterval(id); };
  }, [billingStatusFilter, billingClientFilter]);

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

  useEffect(() => {
    if (!clients?.ok) return;
    const timeline = clients.dailyBiTimeline ?? [];
    const days = timeline.map((d) => d.dayKey);
    if (!days.length) return;
    const today = new Date().toISOString().slice(0, 10);
    const yesterday = addDaysUtc(today, -1);
    const fallback = days.includes(yesterday) ? yesterday : days[days.length - 1];
    const next = selectedDayKey && days.includes(selectedDayKey) ? selectedDayKey : fallback;
    if (next && next !== selectedDayKey) setSelectedDayKey(next);
  }, [clients, selectedDayKey]);

  function reloadClients() {
    fetch("/api/admin/clients", { cache: "no-store" })
      .then((r) => r.json())
      .then((c) => setClients(c))
      .catch(() => { /* noop */ });
  }

  function reloadBilling() {
    const params = new URLSearchParams();
    if (billingStatusFilter !== "all") params.set("status", billingStatusFilter);
    if (billingClientFilter !== "all") params.set("userId", billingClientFilter);
    params.set("limit", "700");
    fetch(`/api/admin/billing/statements?${params.toString()}`, { cache: "no-store" })
      .then((r) => r.json())
      .then((payload) => {
        if (!payload?.ok) {
          setBillingError(payload?.error ?? "No se pudo cargar cobranzas.");
          return;
        }
        setBillingError(null);
        setBilling(payload);
      })
      .catch(() => setBillingError("No se pudo cargar cobranzas."));
  }

  async function generateStatements() {
    setBillingLoading(true);
    setBillingError(null);
    try {
      const payload: Record<string, unknown> = {
        mode: billingMode,
        cutoffDay: billingCutoffDay,
        includeZeroDue: true,
      };
      if (billingClientFilter !== "all") payload.userId = billingClientFilter;
      if (billingMode === "custom") {
        payload.startDay = billingStartDay;
        payload.endDay = billingEndDay;
      }

      const res = await fetch("/api/admin/billing/statements", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok || !data?.ok) {
        setBillingError(data?.error ?? "No se pudieron generar estados de cuenta.");
        return;
      }
      setBilling(data);
      setBillingError(null);
    } catch {
      setBillingError("No se pudieron generar estados de cuenta.");
    } finally {
      setBillingLoading(false);
    }
  }

  async function markStatementStatus(statementId: string, status: BillingStatus) {
    setBillingActionBusyId(statementId);
    try {
      const res = await fetch(`/api/admin/billing/statements/${statementId}/status`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status }),
      });
      const data = await res.json();
      if (!res.ok || !data?.ok) {
        setBillingError(data?.error ?? "No se pudo actualizar el estado de pago.");
      } else {
        setBillingError(null);
        reloadBilling();
      }
    } catch {
      setBillingError("No se pudo actualizar el estado de pago.");
    } finally {
      setBillingActionBusyId(null);
    }
  }

  async function sendStatementEmail(statementId: string) {
    setBillingActionBusyId(statementId);
    try {
      const res = await fetch(`/api/admin/billing/statements/${statementId}/send-email`, {
        method: "POST",
      });
      const data = await res.json();
      if (!res.ok || !data?.ok) {
        setBillingError(data?.error ?? "No se pudo enviar el correo de cobro.");
      } else {
        setBillingError(null);
        reloadBilling();
      }
    } catch {
      setBillingError("No se pudo enviar el correo de cobro.");
    } finally {
      setBillingActionBusyId(null);
    }
  }

  async function saveClientWhatsapp(clientId: string) {
    const value = whatsAppDraftByClient[clientId] ?? "";
    setBillingActionBusyId(clientId);
    try {
      const res = await fetch(`/api/admin/clients/${clientId}/billing-contact`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ billingWhatsApp: value }),
      });
      const data = await res.json();
      if (!res.ok || !data?.ok) {
        setBillingError(data?.error ?? "No se pudo guardar el WhatsApp del cliente.");
      } else {
        setBillingError(null);
        reloadClients();
        reloadBilling();
      }
    } catch {
      setBillingError("No se pudo guardar el WhatsApp del cliente.");
    } finally {
      setBillingActionBusyId(null);
    }
  }

  function openWhatsappForStatement(statement: BillingStatementRow) {
    const nequi = billing?.paymentMethods?.nequi ?? "3206876633";
    const daviKey = billing?.paymentMethods?.daviKey ?? "@DAVI3205046277";
    const target = sanitizeWhatsapp(statement.billingWhatsApp ?? "");
    if (!target) {
      setBillingError(`El cliente ${statement.displayName} no tiene WhatsApp configurado.`);
      return;
    }
    const text = buildWhatsappChargeMessage(statement, nequi, daviKey);
    const url = `https://wa.me/${target}?text=${encodeURIComponent(text)}`;
    window.open(url, "_blank", "noopener,noreferrer");
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
      <Header adminTab={adminTab} onChangeTab={setAdminTab} />

      {adminTab === "control" ? (
        <>
          {/* Seccion 1 — Estado del bot */}
          <BotStatusPanel bot={bot} openCount={openPos?.count ?? 0} />

          {/* Seccion 2 — Tabla de clientes */}
          <ClientsPanel clients={clients} onAdd={() => setShowAdd(true)} />

          {/* Seccion 3 — BI diario por fecha */}
          <DailyControlBiPanel clients={clients} selectedDayKey={selectedDayKey} onChangeDay={setSelectedDayKey} />

          {/* Seccion 4 — Posiciones abiertas del bot maestro */}
          <OpenPositionsPanel positions={openPos?.positions ?? []} />

          {/* Seccion 5 — Metricas del bot */}
          <BotMetricsPanel metrics={metrics} windowSel={windowSel} onChangeWindow={setWindowSel} />

          {/* Seccion 6 — Por simbolo */}
          <SymbolMetricsPanel data={symMetrics} />
        </>
      ) : (
        <BillingHubPanel
          clients={clients?.clients ?? []}
          billing={billing}
          billingStatusFilter={billingStatusFilter}
          onStatusFilterChange={setBillingStatusFilter}
          billingClientFilter={billingClientFilter}
          onClientFilterChange={setBillingClientFilter}
          billingMode={billingMode}
          onModeChange={setBillingMode}
          billingCutoffDay={billingCutoffDay}
          onCutoffDayChange={setBillingCutoffDay}
          billingStartDay={billingStartDay}
          onStartDayChange={setBillingStartDay}
          billingEndDay={billingEndDay}
          onEndDayChange={setBillingEndDay}
          billingLoading={billingLoading}
          billingError={billingError}
          billingActionBusyId={billingActionBusyId}
          whatsAppDraftByClient={whatsAppDraftByClient}
          onWhatsAppDraftChange={(clientId, value) => setWhatsAppDraftByClient((prev) => ({ ...prev, [clientId]: value }))}
          onGenerate={generateStatements}
          onMarkStatus={markStatementStatus}
          onSendEmail={sendStatementEmail}
          onOpenWhatsapp={openWhatsappForStatement}
          onSaveClientWhatsapp={saveClientWhatsapp}
        />
      )}

      {showAdd && (
        <AddClientModal onClose={() => setShowAdd(false)} onCreated={() => { setShowAdd(false); reloadClients(); }} />
      )}
    </div>
  );
}

function Header({
  adminTab,
  onChangeTab,
}: {
  adminTab: "control" | "billing";
  onChangeTab: (tab: "control" | "billing") => void;
}) {
  return (
    <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
      <div>
        <h1 style={{ fontSize: 22, fontWeight: 800, margin: 0, letterSpacing: "-0.02em" }}>
          ◈ OptiFerre <span style={{ color: GREEN }}>{adminTab === "control" ? "Mission Control" : "Cobros & Estados"}</span>
        </h1>
        <p style={{ color: MUTE, fontSize: 11, margin: "4px 0 0", letterSpacing: "0.08em" }}>
          ADMIN · liquidacion transparente y cobranza ejecutiva
        </p>
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <button
          onClick={() => onChangeTab("control")}
          style={{
            background: adminTab === "control" ? BLUE : "transparent",
            color: adminTab === "control" ? "#000" : TEXT,
            border: `1px solid ${BORD}`,
            borderRadius: 9,
            padding: "8px 12px",
            fontSize: 11,
            fontWeight: 700,
            cursor: "pointer",
          }}
        >
          Control operativo
        </button>
        <button
          onClick={() => onChangeTab("billing")}
          style={{
            background: adminTab === "billing" ? AMBER : "transparent",
            color: adminTab === "billing" ? "#000" : TEXT,
            border: `1px solid ${BORD}`,
            borderRadius: 9,
            padding: "8px 12px",
            fontSize: 11,
            fontWeight: 700,
            cursor: "pointer",
          }}
        >
          Cobros y estado de cuenta
        </button>
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
  const totalServiceDueToday = clients?.totalServiceDueTodayUtc ?? clients?.totalAdminFee20LatestDay ?? clients?.totalAdminFee20 ?? 0;
  const totalServiceDueYesterday = clients?.totalServiceDueYesterdayUtc ?? 0;
  const totalPnlYesterday = clients?.totalPnlYesterdayUtc ?? 0;
  const totalProjectedNextDayCapital = clients?.totalProjectedNextDayCapital ?? 0;
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
        <MiniKpi label="PnL ayer (UTC)" value={fmtUSD(totalPnlYesterday, true)} color={totalPnlYesterday >= 0 ? GREEN : RED} />
        <MiniKpi label="Cobro ayer (20%)" value={fmtUSD(totalServiceDueYesterday)} color={AMBER} />
        <MiniKpi label="Cobro hoy (20%)" value={fmtUSD(totalServiceDueToday)} color={AMBER} />
        <MiniKpi label="Capital base manana" value={fmtUSD(totalProjectedNextDayCapital)} color={BLUE} />
      </div>

      {rows.length === 0 ? (
        <p style={{ color: MUTE }}>Sin clientes activos.</p>
      ) : (
        <Table headers={["Cliente", "Balance live", "Inicio conteo", "Capital base hoy", "PnL hoy", "Cobro hoy (20%)", "Cobro ayer (20%)", "Base manana", "Estado"]}>
          {rows.map((c) => (
            <tr key={c.id}>
              <Td>
                <div style={{ display: "flex", flexDirection: "column" }}>
                  <span>{c.displayName || c.email}</span>
                  <span style={{ color: MUTE, fontSize: 10 }}>{c.derivAccountId ?? "sin Deriv"} · {c.balanceSource}</span>
                  <span style={{ color: MUTE, fontSize: 10 }}>{c.tradesSinceStart} trades desde alta</span>
                  {c.balanceError && <span style={{ color: AMBER, fontSize: 10 }}>{c.balanceError}</span>}
                </div>
              </Td>
              <Td mono color={c.balanceSource === "deriv_ws" ? TEXT : c.balanceSource === "cache" ? MUTE : AMBER}>
                {fmtMaybeUSD(c.balanceActual)}
              </Td>
              <Td>
                <div style={{ display: "flex", flexDirection: "column" }}>
                  <span style={{ color: TEXT, fontSize: 12 }}>
                    {c.fechaInicio ? new Date(c.fechaInicio).toLocaleString("es-ES", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "sin fecha_inicio"}
                  </span>
                  <span style={{ color: MUTE, fontSize: 10 }}>
                    {c.firstDayPartial ? "primer dia parcial desde hora de alta" : "dia completo"}
                  </span>
                </div>
              </Td>
              <Td mono color={BLUE}>
                {fmtUSD(c.operationalCapitalToday)}
              </Td>
              <Td mono color={c.realizedPnlTodayUtc >= 0 ? GREEN : RED}>
                {fmtUSD(c.realizedPnlTodayUtc, true)}
              </Td>
              <Td mono color={c.enModoRecuperacion == null ? MUTE : c.enModoRecuperacion ? MUTE : AMBER}>
                {c.enModoRecuperacion == null ? "—" : c.enModoRecuperacion ? fmtUSD(0) : fmtUSD(c.serviceDueTodayUtc)}
              </Td>
              <Td mono color={c.enModoRecuperacion == null ? MUTE : c.enModoRecuperacion ? MUTE : AMBER}>
                {c.enModoRecuperacion == null ? "—" : c.enModoRecuperacion ? fmtUSD(0) : fmtUSD(c.serviceDueYesterdayUtc)}
              </Td>
              <Td mono color={BLUE}>
                {fmtUSD(c.projectedNextDayCapital)}
              </Td>
              <Td color={c.enModoRecuperacion == null ? MUTE : c.enModoRecuperacion ? AMBER : GREEN}>
                {c.enModoRecuperacion == null ? "Sin lectura Deriv" : c.enModoRecuperacion ? "Recuperacion" : "En ganancia"}
              </Td>
            </tr>
          ))}
        </Table>
      )}
      <p style={{ marginTop: 12, color: MUTE, fontSize: 12, lineHeight: 1.5 }}>
        Regla aplicada: cada cliente cobra desde su fecha-hora de alta. El primer dia se cuenta solo desde esa hora.
        Capital base del dia siguiente = capital base de hoy + PnL real de hoy (sin recobrar sobre ganancias de dias previos).
      </p>
    </Card>
  );
}

function BillingHubPanel({
  clients,
  billing,
  billingStatusFilter,
  onStatusFilterChange,
  billingClientFilter,
  onClientFilterChange,
  billingMode,
  onModeChange,
  billingCutoffDay,
  onCutoffDayChange,
  billingStartDay,
  onStartDayChange,
  billingEndDay,
  onEndDayChange,
  billingLoading,
  billingError,
  billingActionBusyId,
  whatsAppDraftByClient,
  onWhatsAppDraftChange,
  onGenerate,
  onMarkStatus,
  onSendEmail,
  onOpenWhatsapp,
  onSaveClientWhatsapp,
}: {
  clients: ClientRow[];
  billing: BillingListResponse | null;
  billingStatusFilter: "all" | BillingStatus;
  onStatusFilterChange: (value: "all" | BillingStatus) => void;
  billingClientFilter: string;
  onClientFilterChange: (value: string) => void;
  billingMode: BillingMode;
  onModeChange: (value: BillingMode) => void;
  billingCutoffDay: string;
  onCutoffDayChange: (value: string) => void;
  billingStartDay: string;
  onStartDayChange: (value: string) => void;
  billingEndDay: string;
  onEndDayChange: (value: string) => void;
  billingLoading: boolean;
  billingError: string | null;
  billingActionBusyId: string | null;
  whatsAppDraftByClient: Record<string, string>;
  onWhatsAppDraftChange: (clientId: string, value: string) => void;
  onGenerate: () => void;
  onMarkStatus: (statementId: string, status: BillingStatus) => Promise<void>;
  onSendEmail: (statementId: string) => Promise<void>;
  onOpenWhatsapp: (statement: BillingStatementRow) => void;
  onSaveClientWhatsapp: (clientId: string) => Promise<void>;
}) {
  const statements = billing?.statements ?? [];
  const totals = billing?.totals ?? {
    pendingAmountUsdt: 0,
    serviceDueUsdt: 0,
    paidAmountUsdt: 0,
    pendingCount: 0,
    paidCount: 0,
    waivedCount: 0,
    clientsWithDebt: 0,
  };
  const paymentNequi = billing?.paymentMethods?.nequi ?? "3206876633";
  const paymentDavi = billing?.paymentMethods?.daviKey ?? "@DAVI3205046277";

  return (
    <Card
      title="Centro de cobranzas y estados de cuenta"
      accent={AMBER}
      action={
        <button
          onClick={onGenerate}
          disabled={billingLoading}
          style={{
            background: billingLoading ? "rgba(255,255,255,0.15)" : AMBER,
            color: billingLoading ? MUTE : "#000",
            border: "none",
            borderRadius: 8,
            padding: "8px 14px",
            fontWeight: 700,
            cursor: billingLoading ? "not-allowed" : "pointer",
            fontSize: 12,
          }}
        >
          {billingLoading ? "Generando..." : "Generar estados de cuenta"}
        </button>
      }
    >
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))", gap: 10, marginBottom: 12 }}>
        <MiniKpi label="Pendiente total" value={fmtUSD(totals.pendingAmountUsdt)} color={totals.pendingAmountUsdt > 0 ? RED : GREEN} />
        <MiniKpi label="Cobrado" value={fmtUSD(totals.paidAmountUsdt)} color={BLUE} />
        <MiniKpi label="Estados pendientes" value={String(totals.pendingCount)} color={AMBER} />
        <MiniKpi label="Clientes con deuda" value={String(totals.clientsWithDebt)} color={totals.clientsWithDebt > 0 ? RED : GREEN} />
      </div>

      <div style={{
        border: `1px solid ${BORD}`,
        borderRadius: 12,
        padding: 12,
        marginBottom: 12,
        background: "linear-gradient(180deg, rgba(13,22,34,0.95) 0%, rgba(7,12,20,0.95) 100%)",
      }}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 8, marginBottom: 8 }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={{ color: MUTE, fontSize: 11 }}>Cliente</label>
            <select
              value={billingClientFilter}
              onChange={(e) => onClientFilterChange(e.target.value)}
              style={selectStyle}
            >
              <option value="all">Todos los clientes</option>
              {clients.map((c) => (
                <option key={c.id} value={c.id}>{c.displayName || c.email}</option>
              ))}
            </select>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={{ color: MUTE, fontSize: 11 }}>Modo de corte</label>
            <select
              value={billingMode}
              onChange={(e) => onModeChange(e.target.value as BillingMode)}
              style={selectStyle}
            >
              <option value="since_last_payment">Desde ultimo pago</option>
              <option value="rolling_7d">Ultimos 7 dias</option>
              <option value="rolling_15d">Ultimos 15 dias</option>
              <option value="custom">Rango manual</option>
            </select>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={{ color: MUTE, fontSize: 11 }}>Estado</label>
            <select
              value={billingStatusFilter}
              onChange={(e) => onStatusFilterChange(e.target.value as "all" | BillingStatus)}
              style={selectStyle}
            >
              <option value="all">Todos</option>
              <option value="pending">Pendiente</option>
              <option value="paid">Pagado</option>
              <option value="waived">Sin cobro</option>
            </select>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={{ color: MUTE, fontSize: 11 }}>Corte hasta dia</label>
            <input type="date" value={billingCutoffDay} onChange={(e) => onCutoffDayChange(e.target.value)} style={inputStyle} />
          </div>

          {billingMode === "custom" && (
            <>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <label style={{ color: MUTE, fontSize: 11 }}>Inicio manual</label>
                <input type="date" value={billingStartDay} onChange={(e) => onStartDayChange(e.target.value)} style={inputStyle} />
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <label style={{ color: MUTE, fontSize: 11 }}>Fin manual</label>
                <input type="date" value={billingEndDay} onChange={(e) => onEndDayChange(e.target.value)} style={inputStyle} />
              </div>
            </>
          )}
        </div>

        <p style={{ margin: 0, color: MUTE, fontSize: 11 }}>
          Modo activo: <span style={{ color: TEXT }}>{modeLabel(billingMode)}</span>. Pago institucional por Nequi <span style={{ color: TEXT }}>{paymentNequi}</span> o llave <span style={{ color: TEXT }}>{paymentDavi}</span>.
        </p>
      </div>

      {billingError && (
        <p style={{ margin: "0 0 10px", color: RED, fontSize: 12 }}>{billingError}</p>
      )}

      {statements.length === 0 ? (
        <p style={{ margin: 0, color: MUTE }}>No hay estados de cuenta para el filtro actual. Genera un corte para poblar la cartera.</p>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <Table headers={["Cliente", "Periodo", "Opero", "PnL", "Servicio 20%", "Pendiente", "Estado", "WhatsApp", "Acciones"]}>
            {statements.map((row) => {
              const clientDraft = whatsAppDraftByClient[row.userId] ?? row.billingWhatsApp ?? "";
              const savingWhatsapp = billingActionBusyId === row.userId;
              const actingOnStatement = billingActionBusyId === row.id;
              const nextStatus: BillingStatus = row.status === "paid" ? "pending" : "paid";

              return (
                <tr key={row.id}>
                  <Td>
                    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                      <span>{row.displayName || row.email}</span>
                      <span style={{ color: MUTE, fontSize: 10 }}>{row.email}</span>
                    </div>
                  </Td>
                  <Td mono>
                    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                      <span>{row.periodStart} a {row.periodEnd}</span>
                      <span style={{ color: MUTE, fontSize: 10 }}>{modeLabel(row.mode)}</span>
                    </div>
                  </Td>
                  <Td mono>{row.tradesCount}</Td>
                  <Td mono color={row.pnlUsdt >= 0 ? GREEN : RED}>{fmtUSD(row.pnlUsdt, true)}</Td>
                  <Td mono color={AMBER}>{fmtUSD(row.serviceDueUsdt)}</Td>
                  <Td mono color={row.pendingAmountUsdt > 0 ? RED : GREEN}>{fmtUSD(row.pendingAmountUsdt)}</Td>
                  <Td>
                    <Badge
                      color={row.status === "paid" ? GREEN : row.status === "pending" ? AMBER : BLUE}
                      text={row.status === "paid" ? "Pagado" : row.status === "pending" ? "Pendiente" : "Sin cobro"}
                    />
                  </Td>
                  <Td>
                    <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                      <input
                        value={clientDraft}
                        onChange={(e) => onWhatsAppDraftChange(row.userId, e.target.value)}
                        placeholder="573001112233"
                        style={{
                          ...inputStyle,
                          minWidth: 130,
                          fontSize: 12,
                          padding: "6px 8px",
                        }}
                      />
                      <button
                        onClick={() => onSaveClientWhatsapp(row.userId)}
                        disabled={savingWhatsapp}
                        style={{
                          background: "transparent",
                          color: TEXT,
                          border: `1px solid ${BORD}`,
                          borderRadius: 7,
                          padding: "6px 8px",
                          fontSize: 11,
                          fontWeight: 700,
                          cursor: savingWhatsapp ? "not-allowed" : "pointer",
                        }}
                      >
                        {savingWhatsapp ? "Guardando" : "Guardar"}
                      </button>
                    </div>
                  </Td>
                  <Td>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                      <button
                        onClick={() => onOpenWhatsapp(row)}
                        disabled={actingOnStatement}
                        style={smallActionButton("#25d366", actingOnStatement)}
                      >
                        WhatsApp
                      </button>
                      <button
                        onClick={() => onSendEmail(row.id)}
                        disabled={actingOnStatement}
                        style={smallActionButton(BLUE, actingOnStatement)}
                      >
                        Correo
                      </button>
                      <button
                        onClick={() => onMarkStatus(row.id, nextStatus)}
                        disabled={actingOnStatement}
                        style={smallActionButton(row.status === "paid" ? AMBER : GREEN, actingOnStatement)}
                      >
                        {row.status === "paid" ? "Marcar pendiente" : "Marcar pagado"}
                      </button>
                    </div>
                  </Td>
                </tr>
              );
            })}
          </Table>
        </div>
      )}

      <p style={{ marginTop: 12, color: MUTE, fontSize: 12, lineHeight: 1.5 }}>
        El valor del servicio (20%) es dinamico por periodo y solo aplica sobre PnL positivo. Si el PnL cae por perdidas, el cobro tambien baja; si el PnL del periodo es negativo, el cobro del servicio queda en cero.
      </p>
    </Card>
  );
}

const selectStyle: React.CSSProperties = {
  background: "rgba(8,15,25,0.95)",
  border: `1px solid ${BORD}`,
  color: TEXT,
  borderRadius: 8,
  padding: "8px 10px",
  fontSize: 12,
};

const inputStyle: React.CSSProperties = {
  background: "rgba(8,15,25,0.95)",
  border: `1px solid ${BORD}`,
  color: TEXT,
  borderRadius: 8,
  padding: "8px 10px",
  fontSize: 12,
};

function smallActionButton(color: string, disabled: boolean): React.CSSProperties {
  return {
    background: disabled ? "rgba(255,255,255,0.15)" : color,
    color: "#000",
    border: "none",
    borderRadius: 7,
    padding: "6px 9px",
    fontSize: 11,
    fontWeight: 700,
    cursor: disabled ? "not-allowed" : "pointer",
  };
}

function DailyControlBiPanel({
  clients,
  selectedDayKey,
  onChangeDay,
}: {
  clients: ClientsResponse | null;
  selectedDayKey: string;
  onChangeDay: (value: string) => void;
}) {
  const timeline = [...(clients?.dailyBiTimeline ?? [])].sort((a, b) => (a.dayKey < b.dayKey ? -1 : 1));
  if (!timeline.length) {
    return (
      <Card title="BI diario por fecha" accent={BLUE}>
        <p style={{ color: MUTE, margin: 0 }}>Aun no hay cierres diarios para construir el centro BI.</p>
      </Card>
    );
  }

  const selected = timeline.find((d) => d.dayKey === selectedDayKey) ?? timeline[timeline.length - 1];
  const minDay = timeline[0].dayKey;
  const maxDay = timeline[timeline.length - 1].dayKey;
  const todayKey = new Date().toISOString().slice(0, 10);
  const yesterdayKey = addDaysUtc(todayKey, -1);

  const selectedClientRows = (clients?.clients ?? [])
    .map((c) => {
      const d = (c.dailySettlement ?? []).find((row) => row.dayKey === selected.dayKey) ?? null;
      return { client: c, day: d };
    })
    .filter((row) => row.day !== null)
    .sort((a, b) => (b.day!.pnl - a.day!.pnl));

  const recentTimeline = timeline.slice(-30);

  return (
    <Card title="BI diario por fecha" accent={BLUE}>
      <div style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 10,
        marginBottom: 12,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ color: MUTE, fontSize: 12 }}>Filtro por dia:</span>
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
          <button onClick={() => onChangeDay(todayKey)} style={adminChipButtonStyle(selected.dayKey === todayKey)}>Hoy</button>
          <button onClick={() => onChangeDay(yesterdayKey)} style={adminChipButtonStyle(selected.dayKey === yesterdayKey)}>Ayer</button>
          <button onClick={() => onChangeDay(maxDay)} style={adminChipButtonStyle(selected.dayKey === maxDay)}>Ultimo cierre</button>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 10, marginBottom: 12 }}>
        <MiniKpi label="Dia" value={new Date(`${selected.dayKey}T00:00:00Z`).toLocaleDateString("es-ES")} />
        <MiniKpi label="PnL del dia" value={fmtUSD(selected.pnl, true)} color={selected.pnl >= 0 ? GREEN : RED} />
        <MiniKpi label="Cobro del dia (20%)" value={fmtUSD(selected.service)} color={AMBER} />
        <MiniKpi label="Neto clientes" value={fmtUSD(selected.clientNet, true)} color={selected.clientNet >= 0 ? GREEN : RED} />
        <MiniKpi label="Capital inicio" value={fmtUSD(selected.capitalStart)} color={BLUE} />
        <MiniKpi label="Capital cierre" value={fmtUSD(selected.capitalEnd)} color={BLUE} />
        <MiniKpi label="Trades" value={String(selected.trades)} />
        <MiniKpi label="Clientes con dato" value={String(selected.clients)} />
      </div>

      <div style={{
        border: `1px solid ${BORD}`,
        borderRadius: 12,
        padding: 12,
        background: "linear-gradient(180deg, rgba(13,22,34,0.95) 0%, rgba(7,12,20,0.95) 100%)",
        marginBottom: 12,
      }}>
        <p style={{ margin: "0 0 8px", color: MUTE, fontSize: 11, letterSpacing: "0.1em", textTransform: "uppercase" }}>
          Curva agregada de capital (ultimos {recentTimeline.length} dias)
        </p>
        <AdminGrowthCurve points={recentTimeline.map((d) => ({ dayKey: d.dayKey, value: d.capitalEnd }))} />
      </div>

      {selectedClientRows.length > 0 && (
        <Table headers={["Cliente", "PnL", "Cobro (20%)", "Neto cliente", "Capital inicio", "Capital cierre", "Trades"]}>
          {selectedClientRows.map((row) => (
            <tr key={`${row.client.id}-${selected.dayKey}`}>
              <Td>{row.client.displayName || row.client.email}</Td>
              <Td mono color={row.day!.pnl >= 0 ? GREEN : RED}>{fmtUSD(row.day!.pnl, true)}</Td>
              <Td mono color={AMBER}>{fmtUSD(row.day!.service)}</Td>
              <Td mono color={row.day!.clientNet >= 0 ? GREEN : RED}>{fmtUSD(row.day!.clientNet, true)}</Td>
              <Td mono color={BLUE}>{fmtUSD(row.day!.capitalStart)}</Td>
              <Td mono color={BLUE}>{fmtUSD(row.day!.capitalEnd)}</Td>
              <Td mono>{String(row.day!.trades)}</Td>
            </tr>
          ))}
        </Table>
      )}
    </Card>
  );
}

function AdminGrowthCurve({ points }: { points: Array<{ dayKey: string; value: number }> }) {
  if (points.length < 2) {
    return <p style={{ margin: 0, color: MUTE, fontSize: 12 }}>Se requieren al menos 2 dias para dibujar la curva.</p>;
  }

  const width = 1000;
  const height = 180;
  const values = points.map((p) => p.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = Math.max(1, max - min);
  const coords = points.map((p, i) => {
    const x = (i / (points.length - 1)) * width;
    const y = height - ((p.value - min) / range) * (height - 20) - 10;
    return { x, y };
  });
  const linePath = coords.map((c, i) => `${i === 0 ? "M" : "L"} ${c.x.toFixed(2)} ${c.y.toFixed(2)}`).join(" ");
  const areaPath = `${linePath} L ${width} ${height} L 0 ${height} Z`;

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} style={{ width: "100%", height: 180, display: "block" }}>
        <defs>
          <linearGradient id="adminGrowthFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="rgba(61,214,255,0.34)" />
            <stop offset="100%" stopColor="rgba(61,214,255,0.03)" />
          </linearGradient>
        </defs>
        <line x1={0} y1={height - 1} x2={width} y2={height - 1} stroke="rgba(138,165,191,0.25)" strokeWidth="1" />
        <path d={areaPath} fill="url(#adminGrowthFill)" />
        <path d={linePath} fill="none" stroke={BLUE} strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <div style={{ display: "flex", justifyContent: "space-between", marginTop: 6, fontSize: 11, color: MUTE }}>
        <span>{new Date(`${points[0].dayKey}T00:00:00Z`).toLocaleDateString("es-ES")}</span>
        <span style={{ color: BLUE, fontFamily: "ui-monospace, Menlo, monospace" }}>{fmtUSD(points[points.length - 1].value)}</span>
      </div>
    </div>
  );
}

function adminChipButtonStyle(active: boolean) {
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
    billingWhatsApp: "",
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
        <Input label="WhatsApp cobro cliente" value={form.billingWhatsApp} onChange={(v) => setForm({ ...form, billingWhatsApp: v })} placeholder="573001112233" />
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
