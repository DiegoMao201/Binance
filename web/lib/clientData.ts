/**
 * lib/clientData.ts — Helpers comunes para acceder a datos de un cliente
 * filtrados SIEMPRE por su fecha_inicio. Usados por el frontend cliente
 * y la vista detallada del frontend admin.
 *
 * Reglas absolutas:
 *  - Nunca se devuelven trades anteriores a fecha_inicio del cliente.
 *  - Las queries son raw SQL para tolerar el esquema hibrido (legacy
 *    Binance + nuevas columnas Deriv) que existe en produccion.
 */

import fs from "node:fs/promises";
import path from "node:path";
import { prisma } from "@/lib/db";
import { Prisma } from "@prisma/client";

export interface ClientCoreProfile {
  id: string;
  email: string;
  displayName: string;
  isActive: boolean;
  capitalInicial: number;
  fechaInicio: Date | null;
  derivToken: string | null;
  derivAccountId: string | null;
  balanceActualCache: number | null;
  balanceActualAt: Date | null;
  comisionTotalCobrada: number;
}

type RawClientRow = {
  id: string;
  email: string;
  display_name: string | null;
  is_active: boolean;
  capital_inicial: Prisma.Decimal | null;
  fecha_inicio: Date | null;
  deriv_token: string | null;
  deriv_account_id: string | null;
  balance_actual_cache: Prisma.Decimal | null;
  balance_actual_at: Date | null;
  comision_total_cobrada: Prisma.Decimal | null;
};

function rowToProfile(row: RawClientRow): ClientCoreProfile {
  return {
    id: row.id,
    email: row.email,
    displayName: row.display_name ?? "",
    isActive: row.is_active,
    capitalInicial: row.capital_inicial ? Number(row.capital_inicial) : 0,
    fechaInicio: row.fecha_inicio,
    derivToken: row.deriv_token,
    derivAccountId: row.deriv_account_id,
    balanceActualCache: row.balance_actual_cache ? Number(row.balance_actual_cache) : null,
    balanceActualAt: row.balance_actual_at,
    comisionTotalCobrada: row.comision_total_cobrada ? Number(row.comision_total_cobrada) : 0,
  };
}

export async function getClientProfile(userId: string): Promise<ClientCoreProfile | null> {
  const rows = await prisma.$queryRaw<RawClientRow[]>`
    SELECT
      u.id::text                                AS id,
      u.email                                   AS email,
      u.display_name                            AS display_name,
      u.is_active                               AS is_active,
      to_jsonb(u)->>'capital_inicial'           AS capital_inicial,
      (to_jsonb(u)->>'fecha_inicio')::timestamptz             AS fecha_inicio,
      to_jsonb(u)->>'deriv_token'               AS deriv_token,
      to_jsonb(u)->>'deriv_account_id'          AS deriv_account_id,
      to_jsonb(u)->>'balance_actual_cache'      AS balance_actual_cache,
      (to_jsonb(u)->>'balance_actual_at')::timestamptz        AS balance_actual_at,
      COALESCE(to_jsonb(u)->>'comision_total_cobrada', '0')   AS comision_total_cobrada
    FROM users u
    WHERE u.id = ${userId}::uuid
    LIMIT 1
  `;
  if (!rows.length) return null;
  return rowToProfile(rows[0]);
}

export async function listActiveClients(): Promise<ClientCoreProfile[]> {
  const rows = await prisma.$queryRaw<RawClientRow[]>`
    SELECT
      u.id::text                                AS id,
      u.email                                   AS email,
      u.display_name                            AS display_name,
      u.is_active                               AS is_active,
      to_jsonb(u)->>'capital_inicial'           AS capital_inicial,
      (to_jsonb(u)->>'fecha_inicio')::timestamptz             AS fecha_inicio,
      to_jsonb(u)->>'deriv_token'               AS deriv_token,
      to_jsonb(u)->>'deriv_account_id'          AS deriv_account_id,
      to_jsonb(u)->>'balance_actual_cache'      AS balance_actual_cache,
      (to_jsonb(u)->>'balance_actual_at')::timestamptz        AS balance_actual_at,
      COALESCE(to_jsonb(u)->>'comision_total_cobrada', '0')   AS comision_total_cobrada
    FROM users u
    WHERE u.role IN ('client', 'investor')
      AND u.is_active = TRUE
    ORDER BY u.created_at ASC
  `;
  return rows.map(rowToProfile);
}

export async function updateBalanceCache(userId: string, balance: number): Promise<void> {
  await prisma.$executeRaw`
    UPDATE users
       SET balance_actual_cache = ${balance}::numeric,
           balance_actual_at    = NOW()
     WHERE id = ${userId}::uuid
  `;
}

// ─── Trades cerrados desde fecha_inicio ─────────────────────────────────────
export interface ClosedTradeRow {
  contractId: string;
  symbol: string;
  side: string;
  openedAt: string;
  closedAt: string;
  realizedPnl: number;
}

type ClosedContractFile = {
  contract_id?: number | string;
  user_id?: string;
  symbol?: string;
  side?: string;
  opened_at_ts?: number | string;
  closed_at_ts?: number | string;
  realized_pnl_usdt?: number | string;
};

function toMs(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value > 1_000_000_000_000 ? value : value * 1000;
  }
  if (typeof value === "string" && value.trim()) {
    const n = Number(value);
    if (Number.isFinite(n)) {
      return n > 1_000_000_000_000 ? n : n * 1000;
    }
    const d = new Date(value);
    if (!Number.isNaN(d.getTime())) return d.getTime();
  }
  return null;
}

async function readJsonArrayFromShared(candidates: string[]): Promise<unknown[]> {
  const logsDir = process.env.DERIV_STATE_DIR
    ?? process.env.BOT_STATE_DIR
    ?? path.join(process.cwd(), "..", "logs");
  for (const fileName of candidates) {
    try {
      const raw = await fs.readFile(path.join(logsDir, fileName), "utf8");
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) return parsed;
    } catch {
      // try next
    }
  }
  return [];
}

/**
 * Lee los trades cerrados aplicables al cliente desde fecha_inicio.
 * Si user_id no esta presente en el archivo, devuelve TODOS los cierres
 * posteriores a fecha_inicio (modelo de cuenta espejo: el cliente recibe
 * la senal del bot maestro). Filtrar por user_id solo cuando exista.
 */
export async function listClosedTradesSinceFechaInicio(
  fechaInicio: Date,
  userId: string | null,
): Promise<ClosedTradeRow[]> {
  const fechaMs = fechaInicio.getTime();
  const raw = await readJsonArrayFromShared(["deriv_closed_contracts.json"]);
  const rows = (raw as ClosedContractFile[])
    .map((r, idx): ClosedTradeRow | null => {
      // Si el archivo trae user_id, respetarlo estrictamente.
      if (userId && typeof r.user_id === "string" && r.user_id !== userId) {
        return null;
      }
      const closedMs = toMs(r.closed_at_ts);
      const openedMs = toMs(r.opened_at_ts) ?? closedMs;
      if (closedMs == null || closedMs < fechaMs) return null;

      let pnl = 0;
      try { pnl = Number(r.realized_pnl_usdt ?? 0); } catch { pnl = 0; }
      if (!Number.isFinite(pnl)) pnl = 0;

      return {
        contractId: String(r.contract_id ?? `idx-${idx}`),
        symbol: typeof r.symbol === "string" ? r.symbol : "DERIV",
        side: typeof r.side === "string" ? r.side : "?",
        openedAt: new Date(openedMs ?? closedMs).toISOString(),
        closedAt: new Date(closedMs).toISOString(),
        realizedPnl: pnl,
      };
    })
    .filter((x): x is ClosedTradeRow => x !== null)
    .sort((a, b) => new Date(b.closedAt).getTime() - new Date(a.closedAt).getTime());

  return rows;
}

export async function listOpenContractsFiltered(userId: string | null): Promise<Array<Record<string, unknown>>> {
  const raw = await readJsonArrayFromShared(["deriv_open_contracts.json", "open_positions.json"]);
  const list = raw as Array<Record<string, unknown>>;
  const hasScoped = list.some((r) => typeof r.user_id === "string");
  if (userId && hasScoped) {
    return list.filter((r) => r.user_id === userId);
  }
  return list;
}

export async function readBotStatus(): Promise<Record<string, unknown> | null> {
  const logsDir = process.env.DERIV_STATE_DIR
    ?? process.env.BOT_STATE_DIR
    ?? path.join(process.cwd(), "..", "logs");
  try {
    const raw = await fs.readFile(path.join(logsDir, "deriv_status.json"), "utf8");
    return JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return null;
  }
}
