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
});

const DecimalStringSchema = z
  .string({ required_error: "El monto es obligatorio." })
  .regex(/^\d+(\.\d{1,8})?$/, "Introduce un número válido.");

const MAX_BALANCE = new Prisma.Decimal("10000000");
const FIXED_INITIAL_BALANCE = new Prisma.Decimal("100");
const DEFAULT_PERFORMANCE_FEE_PCT = new Prisma.Decimal("0.20");

// ─── Return type ──────────────────────────────────────────────────────────────

export type CreateInvestorState = {
  success: boolean;
  error?: string;
  fieldErrors?: Partial<Record<"name" | "email", string[]>>;
};

export type AdminMutationResult = {
  success: boolean;
  error?: string;
};

async function requireAdminRole(): Promise<boolean> {
  const cookieStore = await cookies();
  const token = cookieStore.get("auth_token")?.value;
  const payload = token ? await verifyJWT(token) : null;
  return payload?.role === "admin";
}

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
  if (!(await requireAdminRole())) {
    return { success: false, error: "Acceso denegado." };
  }

  // ── 2. Input validation ──────────────────────────────────────────────────────
  const raw = {
    name:            formData.get("name"),
    email:           formData.get("email"),
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

  const { name, email } = parsed.data;

  // ── 3. ACID transaction ──────────────────────────────────────────────────────
  try {
    const depositDec = FIXED_INITIAL_BALANCE;

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
          entryFeePct:       new Prisma.Decimal("0.00"),
          performanceFeePct: DEFAULT_PERFORMANCE_FEE_PCT,
          balanceUsdt:       depositDec,
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
          description: `Capital inicial fijo $100.00 — ${name}`,
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

export async function setInvestorBalance(
  userId: string,
  targetBalanceRaw: string,
): Promise<AdminMutationResult> {
  if (!(await requireAdminRole())) {
    return { success: false, error: "Acceso denegado." };
  }

  const uid = z.string().uuid().safeParse(userId);
  if (!uid.success) {
    return { success: false, error: "ID de inversor inválido." };
  }

  const parsedAmount = DecimalStringSchema.safeParse(targetBalanceRaw);
  if (!parsedAmount.success) {
    return { success: false, error: "Balance inválido." };
  }

  const target = new Prisma.Decimal(parsedAmount.data);
  if (target.lt(0) || target.gt(MAX_BALANCE)) {
    return { success: false, error: "Balance fuera de rango permitido." };
  }

  try {
    await prisma.$transaction(async (tx) => {
      const user = await tx.user.findFirst({
        where: {
          id: uid.data,
          role: { in: ["client", "investor"] },
        },
        select: { id: true, balanceUsdt: true, isActive: true },
      });
      if (!user) {
        throw new Error("USER_NOT_FOUND");
      }

      const before = new Prisma.Decimal(user.balanceUsdt);
      const delta = target.sub(before);

      await tx.user.update({
        where: { id: uid.data },
        data: { balanceUsdt: target },
      });

      if (delta.gt(0)) {
        await tx.ledgerTransaction.create({
          data: {
            userId: uid.data,
            type: "DEPOSIT",
            amountUsdt: delta,
            description: `Ajuste manual admin: ${before.toFixed(2)} -> ${target.toFixed(2)}`,
          },
        });
      } else if (delta.lt(0)) {
        await tx.ledgerTransaction.create({
          data: {
            userId: uid.data,
            type: "WITHDRAWAL",
            amountUsdt: delta.abs(),
            description: `Ajuste manual admin: ${before.toFixed(2)} -> ${target.toFixed(2)}`,
          },
        });
      }
    });

    revalidatePath("/admin/dashboard");
    return { success: true };
  } catch (err: unknown) {
    if (err instanceof Error && err.message === "USER_NOT_FOUND") {
      return { success: false, error: "Inversor no encontrado." };
    }
    console.error("[setInvestorBalance] Error inesperado:", err);
    return { success: false, error: "No se pudo actualizar el balance." };
  }
}

export async function deactivateInvestor(userId: string): Promise<AdminMutationResult> {
  if (!(await requireAdminRole())) {
    return { success: false, error: "Acceso denegado." };
  }

  const uid = z.string().uuid().safeParse(userId);
  if (!uid.success) {
    return { success: false, error: "ID de inversor inválido." };
  }

  try {
    await prisma.$transaction(async (tx) => {
      const user = await tx.user.findFirst({
        where: {
          id: uid.data,
          role: { in: ["client", "investor"] },
        },
        select: { id: true, isActive: true, balanceUsdt: true },
      });

      if (!user) {
        throw new Error("USER_NOT_FOUND");
      }
      if (!user.isActive) {
        return;
      }

      const balance = new Prisma.Decimal(user.balanceUsdt);

      await tx.user.update({
        where: { id: uid.data },
        data: {
          isActive: false,
          balanceUsdt: new Prisma.Decimal(0),
        },
      });

      if (balance.gt(0)) {
        await tx.ledgerTransaction.create({
          data: {
            userId: uid.data,
            type: "WITHDRAWAL",
            amountUsdt: balance,
            description: "Cierre de cuenta por admin (desactivada)",
          },
        });
      }
    });

    revalidatePath("/admin/dashboard");
    return { success: true };
  } catch (err: unknown) {
    if (err instanceof Error && err.message === "USER_NOT_FOUND") {
      return { success: false, error: "Inversor no encontrado." };
    }
    console.error("[deactivateInvestor] Error inesperado:", err);
    return { success: false, error: "No se pudo desactivar la cuenta." };
  }
}
