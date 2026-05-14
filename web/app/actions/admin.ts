"use server";
/**
 * app/actions/admin.ts — Admin Server Actions
 *
 * Security model (Zero-Trust, server-side):
 *   • Every action verifies the JWT from the auth_token cookie and asserts
 *     role === 'admin'. Middleware is a first defence; actions are the last.
 *   • Input is validated with Zod before any DB call.
 *   • The onboarding transaction is ACID (Prisma $transaction + rollback).
 *
 * Financial precision:
 *   • All monetary arithmetic uses Prisma.Decimal (decimal.js).
 *   • Input from FormData is coerced to string → Decimal. Never to `number`
 *     to avoid IEEE 754 rounding on large USDT amounts.
 */

import { cookies } from "next/headers";
import { revalidatePath } from "next/cache";
import { z } from "zod";
import { Prisma } from "@prisma/client";
import { prisma } from "@/lib/db";
import { verifyJWT } from "@/lib/auth";

// ─── Zod schema ───────────────────────────────────────────────────────────────

const CreateInvestorSchema = z.object({
  name: z
    .string({ required_error: "El nombre es obligatorio." })
    .min(2, "Mínimo 2 caracteres.")
    .max(120, "Máximo 120 caracteres.")
    .trim(),
  email: z
    .string({ required_error: "El email es obligatorio." })
    .email("Formato de email inválido.")
    .max(320, "Email demasiado largo.")
    .toLowerCase()
    .trim(),
  // Accept the string from FormData and validate as a positive decimal.
  // Using z.string() → .refine() preserves precision for Decimal construction.
  initial_deposit: z
    .string({ required_error: "El depósito es obligatorio." })
    .regex(/^\d+(\.\d{1,8})?$/, "Introduce un número válido.")
    .refine(
      (v) => new Prisma.Decimal(v).gt(0),
      "El depósito debe ser mayor a 0."
    )
    .refine(
      (v) => new Prisma.Decimal(v).lte(new Prisma.Decimal("10000000")),
      "El depósito no puede superar 10,000,000 USDT."
    ),
});

// ─── Return type ──────────────────────────────────────────────────────────────

export type CreateInvestorState = {
  success: boolean;
  error?: string;
  fieldErrors?: Partial<Record<"name" | "email" | "initial_deposit", string[]>>;
};

// ─── Server Action ────────────────────────────────────────────────────────────

/**
 * createInvestor — onboards a new PAMM client.
 *
 * Flow:
 *   1. Auth: verify JWT cookie → role must be 'admin'.
 *   2. Validate: Zod schema on raw FormData values.
 *   3. ACID transaction:
 *        a. Check email uniqueness (explicit, better error message than P2002).
 *        b. Create users row: role='client', balanceUsdt = deposit × 0.98.
 *        c. Insert DEPOSIT ledger entry  (gross amount).
 *        d. Insert ENTRY_FEE ledger entry (2% admin fee).
 *   4. Revalidate /admin/dashboard so the Server Component refetches KPIs.
 */
export async function createInvestor(
  prevState: CreateInvestorState,
  formData: FormData
): Promise<CreateInvestorState> {
  // ── 1. Auth guard ────────────────────────────────────────────────────────────
  const cookieStore = await cookies();
  const token = cookieStore.get("auth_token")?.value;
  const payload = token ? await verifyJWT(token) : null;

  if (!payload || payload.role !== "admin") {
    return { success: false, error: "Acceso denegado." };
  }

  // ── 2. Input validation ──────────────────────────────────────────────────────
  const raw = {
    name:            formData.get("name"),
    email:           formData.get("email"),
    initial_deposit: formData.get("initial_deposit"),
  };

  const parsed = CreateInvestorSchema.safeParse(raw);
  if (!parsed.success) {
    const flat = parsed.error.flatten().fieldErrors;
    return {
      success: false,
      error: "Datos inválidos. Revisa los campos.",
      fieldErrors: flat as CreateInvestorState["fieldErrors"],
    };
  }

  const { name, email, initial_deposit } = parsed.data;

  // ── 3. ACID transaction ──────────────────────────────────────────────────────
  try {
    // Use string constructor to avoid IEEE 754 representation issues.
    const depositDec  = new Prisma.Decimal(initial_deposit);
    const entryFee    = depositDec.mul("0.02");
    const clientBal   = depositDec.mul("0.98");

    await prisma.$transaction(async (tx) => {
      // 3a. Explicit uniqueness check → friendly error message.
      const existing = await tx.user.findUnique({
        where: { email },
        select: { id: true },
      });
      if (existing) {
        throw new Error("EMAIL_EXISTS");
      }

      // 3b. Create user.
      const user = await tx.user.create({
        data: {
          name,
          email,
          role:              "client",
          entryFeePct:       new Prisma.Decimal("0.02"),
          performanceFeePct: new Prisma.Decimal("0.05"),
          balanceUsdt:       clientBal,
          isActive:          true,
        },
        select: { id: true },
      });

      // 3c. DEPOSIT ledger entry — gross amount wired by the client.
      await tx.ledgerTransaction.create({
        data: {
          userId:      user.id,
          type:        "DEPOSIT",
          amountUsdt:  depositDec,
          description: `Depósito inicial — ${name}`,
        },
      });

      // 3d. ENTRY_FEE ledger entry — 2% fee retained by admin.
      await tx.ledgerTransaction.create({
        data: {
          userId:      user.id,
          type:        "ENTRY_FEE",
          amountUsdt:  entryFee,
          description: `Entry fee 2% — ${name}`,
        },
      });
    });

    // ── 4. Revalidate dashboard so KPIs refresh ──────────────────────────────
    revalidatePath("/admin/dashboard");
    return { success: true };
  } catch (err: unknown) {
    if (err instanceof Error && err.message === "EMAIL_EXISTS") {
      return {
        success: false,
        error: "Ya existe un cliente registrado con ese email.",
      };
    }
    console.error("[createInvestor] Error inesperado:", err);
    return {
      success: false,
      error: "Error interno al crear el inversor. Intenta de nuevo.",
    };
  }
}
