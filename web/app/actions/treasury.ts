"use server";
/**
 * app/actions/treasury.ts — Treasury Server Actions
 *
 * Security model (Zero-Trust, server-side):
 *   • Every action verifies the JWT from the auth_token cookie and asserts
 *     role === 'admin'. Middleware is a first defence; actions are the last.
 *   • All monetary arithmetic uses Prisma.Decimal for exact precision.
 *
 * Financial rules:
 *   • addCapital   — Top-up: charges 2% Entry Fee. Net capital = amount × 0.98.
 *   • withdrawCapital — Withdrawal: no fee. Fails if amount > balance.
 */

import { cookies } from "next/headers";
import { revalidatePath } from "next/cache";
import { Prisma } from "@prisma/client";
import { prisma } from "@/lib/db";
import { verifyJWT } from "@/lib/auth";

// ─── Shared return type ───────────────────────────────────────────────────────

export type TreasuryResult = {
  success: boolean;
  error?: string;
};

// ─── Constants ────────────────────────────────────────────────────────────────

const ENTRY_FEE_PCT = new Prisma.Decimal("0.02");
const MAX_AMOUNT    = new Prisma.Decimal("10000000");

// ─── Helpers ──────────────────────────────────────────────────────────────────

async function requireAdmin(): Promise<boolean> {
  const cookieStore = await cookies();
  const token = cookieStore.get("auth_token")?.value;
  const payload = token ? await verifyJWT(token) : null;
  return payload?.role === "admin";
}

function parseAmount(raw: string): Prisma.Decimal | null {
  try {
    const d = new Prisma.Decimal(raw.trim());
    if (d.lte(0) || d.gt(MAX_AMOUNT)) return null;
    return d;
  } catch {
    return null;
  }
}

// ─── addCapital ───────────────────────────────────────────────────────────────

/**
 * Registers a new capital top-up for an existing investor.
 *
 * Flow:
 *   1. Auth guard (admin only).
 *   2. Parse & validate amount (Decimal, > 0, ≤ 10M).
 *   3. ACID transaction:
 *        a. Verify user exists and is active.
 *        b. Compute entry fee (2%) and net capital (98%).
 *        c. Increment users.balance_usdt by net capital.
 *        d. Insert DEPOSIT ledger entry (gross amount).
 *        e. Insert ENTRY_FEE ledger entry (2%).
 *   4. Revalidate /admin/dashboard.
 */
export async function addCapital(
  userId: string,
  rawAmount: string
): Promise<TreasuryResult> {
  if (!(await requireAdmin())) {
    return { success: false, error: "Acceso denegado." };
  }

  const amount = parseAmount(rawAmount);
  if (!amount) {
    return { success: false, error: "Monto inválido. Introduce un número positivo." };
  }

  const entryFee   = amount.mul(ENTRY_FEE_PCT);
  const netCapital = amount.sub(entryFee);

  try {
    await prisma.$transaction(async (tx) => {
      const user = await tx.user.findUnique({
        where: { id: userId },
        select: { id: true, isActive: true },
      });
      if (!user || !user.isActive) throw new Error("USER_NOT_FOUND");

      await tx.user.update({
        where: { id: userId },
        data: { balanceUsdt: { increment: netCapital } },
      });

      await tx.ledgerTransaction.create({
        data: {
          userId,
          type:        "DEPOSIT",
          amountUsdt:  amount,
          description: `Top-up $${amount.toFixed(2)} bruto (Fee: $${entryFee.toFixed(2)})`,
        },
      });

      await tx.ledgerTransaction.create({
        data: {
          userId,
          type:        "ENTRY_FEE",
          amountUsdt:  entryFee,
          description: `Entry fee 2% — Top-up $${amount.toFixed(2)}`,
        },
      });
    });

    revalidatePath("/admin/dashboard");
    return { success: true };
  } catch (err: unknown) {
    if (err instanceof Error && err.message === "USER_NOT_FOUND") {
      return { success: false, error: "Inversor no encontrado o inactivo." };
    }
    console.error("[addCapital] Error inesperado:", err);
    return { success: false, error: "Error interno al registrar el aporte. Intenta de nuevo." };
  }
}

// ─── withdrawCapital ──────────────────────────────────────────────────────────

/**
 * Registers a capital withdrawal for an existing investor.
 *
 * Flow:
 *   1. Auth guard (admin only).
 *   2. Parse & validate amount.
 *   3. ACID transaction:
 *        a. Read current balance — verify amount ≤ balance (no overdraft).
 *        b. Decrement users.balance_usdt.
 *        c. Insert WITHDRAWAL ledger entry.
 *   4. Revalidate /admin/dashboard.
 */
export async function withdrawCapital(
  userId: string,
  rawAmount: string
): Promise<TreasuryResult> {
  if (!(await requireAdmin())) {
    return { success: false, error: "Acceso denegado." };
  }

  const amount = parseAmount(rawAmount);
  if (!amount) {
    return { success: false, error: "Monto inválido. Introduce un número positivo." };
  }

  try {
    await prisma.$transaction(async (tx) => {
      const user = await tx.user.findUnique({
        where: { id: userId },
        select: { id: true, isActive: true, balanceUsdt: true },
      });
      if (!user || !user.isActive) throw new Error("USER_NOT_FOUND");

      const balance = new Prisma.Decimal(user.balanceUsdt);
      if (amount.gt(balance)) throw new Error("INSUFFICIENT_BALANCE");

      await tx.user.update({
        where: { id: userId },
        data: { balanceUsdt: balance.sub(amount) },
      });

      await tx.ledgerTransaction.create({
        data: {
          userId,
          type:        "WITHDRAWAL",
          amountUsdt:  amount,
          description: `Retiro $${amount.toFixed(2)}`,
        },
      });
    });

    revalidatePath("/admin/dashboard");
    return { success: true };
  } catch (err: unknown) {
    if (err instanceof Error && err.message === "USER_NOT_FOUND") {
      return { success: false, error: "Inversor no encontrado o inactivo." };
    }
    if (err instanceof Error && err.message === "INSUFFICIENT_BALANCE") {
      return { success: false, error: "Balance insuficiente para este retiro." };
    }
    console.error("[withdrawCapital] Error inesperado:", err);
    return { success: false, error: "Error interno al registrar el retiro. Intenta de nuevo." };
  }
}
