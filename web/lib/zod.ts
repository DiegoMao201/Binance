import { z } from "zod";

// ─── Request OTP ──────────────────────────────────────────────────────────────
export const RequestOtpSchema = z.object({
  email: z
    .string({ required_error: "El email es obligatorio." })
    .email("Formato de email inválido.")
    .max(320, "Email demasiado largo.")
    .toLowerCase()
    .trim(),
});

// ─── Verify OTP ───────────────────────────────────────────────────────────────
export const VerifyOtpSchema = z.object({
  email: z
    .string({ required_error: "El email es obligatorio." })
    .email("Formato de email inválido.")
    .max(320, "Email demasiado largo.")
    .toLowerCase()
    .trim(),
  code: z
    .string({ required_error: "El código OTP es obligatorio." })
    .regex(/^\d{6}$/, "El código debe ser exactamente 6 dígitos numéricos."),
});

// ─── Inferred types ───────────────────────────────────────────────────────────
export type RequestOtpInput = z.infer<typeof RequestOtpSchema>;
export type VerifyOtpInput = z.infer<typeof VerifyOtpSchema>;
