import type { ClientCoreProfile, ClosedTradeRow } from "@/lib/clientData";

export const BILLING_NEQUI_NUMBER = "3206876633";
export const BILLING_DAVI_KEY = "@DAVI3205046277";

export type BillingMode = "rolling_7d" | "rolling_15d" | "since_last_payment" | "custom";
export type BillingStatus = "pending" | "paid" | "waived";

export type DailySettlementRow = {
  dayKey: string;
  pnl: number;
  service: number;
  clientNet: number;
  capitalStart: number;
  capitalEnd: number;
  trades: number;
  partialDay: boolean;
};

export type BillingPeriodSummary = {
  periodStart: string;
  periodEnd: string;
  tradesCount: number;
  pnlUsdt: number;
  serviceDueUsdt: number;
  clientNetUsdt: number;
  capitalStartUsdt: number;
  capitalEndUsdt: number;
  partialStartDay: boolean;
};

const DAY_KEY_RE = /^\d{4}-\d{2}-\d{2}$/;

export function isValidDayKey(value: string): boolean {
  return DAY_KEY_RE.test(value);
}

export function dayKeyFromIso(iso: string): string {
  return iso.slice(0, 10);
}

export function addDaysUtc(dayKeyUtc: string, deltaDays: number): string {
  const d = new Date(`${dayKeyUtc}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + deltaDays);
  return d.toISOString().slice(0, 10);
}

export function dayKeyTodayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

export function defaultBillingCutoffDayUtc(): string {
  return addDaysUtc(dayKeyTodayUtc(), -1);
}

export function buildDailySettlement(
  profile: ClientCoreProfile,
  trades: ClosedTradeRow[],
): DailySettlementRow[] {
  if (!profile.fechaInicio) return [];

  const byDay = new Map<string, { pnl: number; trades: number }>();
  for (const t of trades) {
    const k = dayKeyFromIso(t.closedAt);
    const item = byDay.get(k) ?? { pnl: 0, trades: 0 };
    item.pnl += t.realizedPnl;
    item.trades += 1;
    byDay.set(k, item);
  }

  const orderedDays = Array.from(byDay.entries()).sort((a, b) => (a[0] < b[0] ? -1 : 1));
  const firstDay = profile.fechaInicio.toISOString().slice(0, 10);

  let runningCapital = profile.capitalInicial;
  const daily: DailySettlementRow[] = [];

  for (const [dayKey, value] of orderedDays) {
    const capitalStart = runningCapital;
    const service = Math.max(value.pnl, 0) * 0.2;
    const clientNet = value.pnl - service;
    const capitalEnd = capitalStart + value.pnl;

    daily.push({
      dayKey,
      pnl: value.pnl,
      service,
      clientNet,
      capitalStart,
      capitalEnd,
      trades: value.trades,
      partialDay: dayKey === firstDay,
    });

    runningCapital = capitalEnd;
  }

  return daily;
}

export function resolvePeriodWindow(input: {
  mode: BillingMode;
  fechaInicioDay: string;
  cutoffDay: string;
  customStartDay?: string | null;
  customEndDay?: string | null;
  lastPaidEndDay?: string | null;
}): { periodStart: string; periodEnd: string } | null {
  const { mode, fechaInicioDay, cutoffDay, customStartDay, customEndDay, lastPaidEndDay } = input;

  let periodEnd = cutoffDay;
  let periodStart = cutoffDay;

  if (mode === "rolling_7d") {
    periodStart = addDaysUtc(cutoffDay, -6);
  } else if (mode === "rolling_15d") {
    periodStart = addDaysUtc(cutoffDay, -14);
  } else if (mode === "since_last_payment") {
    periodStart = lastPaidEndDay ? addDaysUtc(lastPaidEndDay, 1) : fechaInicioDay;
  } else {
    periodStart = customStartDay ?? fechaInicioDay;
    periodEnd = customEndDay ?? cutoffDay;
  }

  if (periodStart < fechaInicioDay) periodStart = fechaInicioDay;
  if (periodEnd < periodStart) return null;

  return { periodStart, periodEnd };
}

export function summarizeBillingPeriod(input: {
  profile: ClientCoreProfile;
  dailySettlement: DailySettlementRow[];
  periodStart: string;
  periodEnd: string;
}): BillingPeriodSummary {
  const { profile, dailySettlement, periodStart, periodEnd } = input;

  const daysInPeriod = dailySettlement
    .filter((d) => d.dayKey >= periodStart && d.dayKey <= periodEnd)
    .sort((a, b) => (a.dayKey < b.dayKey ? -1 : 1));

  const pnlBeforePeriod = dailySettlement
    .filter((d) => d.dayKey < periodStart)
    .reduce((acc, d) => acc + d.pnl, 0);

  const pnlUsdt = daysInPeriod.reduce((acc, d) => acc + d.pnl, 0);
  const serviceDueUsdt = daysInPeriod.reduce((acc, d) => acc + d.service, 0);
  const clientNetUsdt = daysInPeriod.reduce((acc, d) => acc + d.clientNet, 0);
  const tradesCount = daysInPeriod.reduce((acc, d) => acc + d.trades, 0);
  const capitalStartUsdt = profile.capitalInicial + pnlBeforePeriod;
  const capitalEndUsdt = capitalStartUsdt + pnlUsdt;

  return {
    periodStart,
    periodEnd,
    tradesCount,
    pnlUsdt,
    serviceDueUsdt,
    clientNetUsdt,
    capitalStartUsdt,
    capitalEndUsdt,
    partialStartDay: profile.fechaInicio?.toISOString().slice(0, 10) === periodStart,
  };
}

export function sanitizeWhatsappNumber(raw: string): string {
  const cleaned = raw.replace(/[^\d+]/g, "").trim();
  if (!cleaned) return "";
  const withoutPlus = cleaned.startsWith("+") ? cleaned.slice(1) : cleaned;
  return withoutPlus;
}

export type BillingMessageInput = {
  clientName: string;
  periodStart: string;
  periodEnd: string;
  tradesCount: number;
  pnlUsdt: number;
  serviceDueUsdt: number;
  paidAmountUsdt?: number;
};

function fmtSigned(value: number): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}`;
}

export function composeBillingWhatsappMessage(input: BillingMessageInput): string {
  const pending = Math.max(input.serviceDueUsdt - (input.paidAmountUsdt ?? 0), 0);
  return [
    `Hola ${input.clientName},`,
    "",
    "Tu estado de cuenta OptiFerre esta listo.",
    `Corte: ${input.periodStart} a ${input.periodEnd} (UTC)`,
    `Operaciones cerradas: ${input.tradesCount}`,
    `PnL del periodo: ${fmtSigned(input.pnlUsdt)} USDT`,
    `Valor servicio (20%): ${input.serviceDueUsdt.toFixed(2)} USDT`,
    `Saldo pendiente: ${pending.toFixed(2)} USDT`,
    "",
    "Puedes realizar el pago por:",
    `Nequi: ${BILLING_NEQUI_NUMBER}`,
    `Llave Davivienda: ${BILLING_DAVI_KEY}`,
    "",
    "Cuando hagas el pago, responde con el soporte para marcar tu cuenta como pagada.",
  ].join("\n");
}
