"use client";
/**
 * app/portal/login/page.tsx — Passwordless OTP Login
 *
 * Two-step flow:
 *   Step 1: Enter email → POST /api/auth/request-otp
 *   Step 2: Enter 6-digit OTP → POST /api/auth/verify-otp
 *   On success: redirect to ?next param or /client/dashboard
 */

import { useState, useTransition, useRef, useEffect, Suspense } from "react";
import { useSearchParams } from "next/navigation";

// ─── Palette (shared with rest of portal) ────────────────────────────────────
const BG    = "#080e16";
const CARD  = "#0a1018";
const BORD  = "#1a2b3c";
const INPUT = "#0e1520";
const TEXT  = "#dce7f5";
const MUTE  = "#6b8299";
const GREEN = "#12d98b";
const RED   = "#eb4b61";

type Step = "email" | "otp";

// Inner component: contains useSearchParams — MUST be inside <Suspense>.
function LoginForm() {
  const searchParams   = useSearchParams();
  const nextPath       = searchParams.get("next") || "/client/dashboard";

  const [step, setStep]               = useState<Step>("email");
  const [email, setEmail]             = useState("");
  const [otp, setOtp]                 = useState("");
  const [errorMsg, setErrorMsg]       = useState("");
  const [successMsg, setSuccessMsg]   = useState("");
  const [isPending, startTransition]  = useTransition();
  const otpRef = useRef<HTMLInputElement>(null);

  // Auto-focus OTP input when step changes
  useEffect(() => {
    if (step === "otp") otpRef.current?.focus();
  }, [step]);

  // ── Step 1: request OTP ─────────────────────────────────────────────────
  async function handleRequestOtp(e: React.FormEvent) {
    e.preventDefault();
    setErrorMsg("");
    startTransition(async () => {
      try {
        const res = await fetch("/api/auth/request-otp", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email }),
        });
        const data = await res.json();
        if (!res.ok) {
          setErrorMsg(data.error || "Error al enviar el código.");
          return;
        }
        setSuccessMsg(`Código enviado a ${email}. Revisa tu bandeja de entrada.`);
        setStep("otp");
      } catch {
        setErrorMsg("Error de red. Verifica tu conexión.");
      }
    });
  }

  // ── Step 2: verify OTP ──────────────────────────────────────────────────
  async function handleVerifyOtp(e: React.FormEvent) {
    e.preventDefault();
    setErrorMsg("");
    startTransition(async () => {
      try {
        const res = await fetch("/api/auth/verify-otp", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email, code: otp }),
        });
        const data = await res.json();
        if (!res.ok) {
          setErrorMsg(data.error || "Código inválido o expirado.");
          return;
        }
        // Cookie is set by the server — redirect now
        window.location.href = nextPath;
      } catch {
        setErrorMsg("Error de red. Verifica tu conexión.");
      }
    });
  }

  return (
    <div
      style={{
        minHeight: "100vh",
        background: BG,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace",
        padding: 20,
      }}
    >
      <div
        style={{
          background: CARD,
          border: `1px solid ${BORD}`,
          borderRadius: 20,
          padding: "36px 32px",
          width: "100%",
          maxWidth: 380,
        }}
      >
        {/* Logo / Header */}
        <div style={{ textAlign: "center", marginBottom: 32 }}>
          <div
            style={{
              width: 48,
              height: 48,
              background: `${GREEN}1a`,
              border: `1px solid ${GREEN}44`,
              borderRadius: 14,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              margin: "0 auto 16px",
              fontSize: 22,
            }}
          >
            ◈
          </div>
          <h1 style={{ color: TEXT, fontSize: 18, fontWeight: 700, margin: 0 }}>
            OptiFerre Portal
          </h1>
          <p style={{ color: MUTE, fontSize: 12, marginTop: 6 }}>
            {step === "email"
              ? "Ingresa tu email para continuar"
              : "Ingresa el código de 6 dígitos"}
          </p>
        </div>

        {/* Step indicator */}
        <div style={{ display: "flex", gap: 6, marginBottom: 28 }}>
          {(["email", "otp"] as Step[]).map((s, i) => (
            <div
              key={s}
              style={{
                flex: 1,
                height: 3,
                borderRadius: 2,
                background:
                  step === s
                    ? GREEN
                    : i < ["email", "otp"].indexOf(step)
                    ? `${GREEN}55`
                    : BORD,
                transition: "background 0.3s",
              }}
            />
          ))}
        </div>

        {/* Form */}
        {step === "email" ? (
          <form onSubmit={handleRequestOtp} style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <div>
              <label style={{ color: MUTE, fontSize: 11, fontWeight: 500, display: "block", marginBottom: 6 }}>
                Email registrado
              </label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="tu@email.com"
                required
                autoFocus
                disabled={isPending}
                style={{
                  width: "100%",
                  background: INPUT,
                  border: `1px solid ${BORD}`,
                  borderRadius: 10,
                  color: TEXT,
                  fontSize: 14,
                  padding: "11px 14px",
                  outline: "none",
                  boxSizing: "border-box",
                  opacity: isPending ? 0.6 : 1,
                }}
              />
            </div>
            <SubmitBtn label="Enviar código →" pending={isPending} />
          </form>
        ) : (
          <form onSubmit={handleVerifyOtp} style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <div>
              <label style={{ color: MUTE, fontSize: 11, fontWeight: 500, display: "block", marginBottom: 6 }}>
                Código OTP (6 dígitos)
              </label>
              <input
                ref={otpRef}
                type="text"
                inputMode="numeric"
                pattern="[0-9]{6}"
                maxLength={6}
                value={otp}
                onChange={(e) => setOtp(e.target.value.replace(/\D/g, "").slice(0, 6))}
                placeholder="000000"
                required
                disabled={isPending}
                style={{
                  width: "100%",
                  background: INPUT,
                  border: `1px solid ${BORD}`,
                  borderRadius: 10,
                  color: TEXT,
                  fontSize: 22,
                  fontWeight: 700,
                  fontFamily: "monospace",
                  letterSpacing: "0.3em",
                  textAlign: "center",
                  padding: "11px 14px",
                  outline: "none",
                  boxSizing: "border-box",
                  opacity: isPending ? 0.6 : 1,
                }}
              />
            </div>
            <SubmitBtn label="Verificar código →" pending={isPending} />
            <button
              type="button"
              onClick={() => { setStep("email"); setOtp(""); setErrorMsg(""); setSuccessMsg(""); }}
              style={{
                background: "none",
                border: "none",
                color: MUTE,
                fontSize: 12,
                cursor: "pointer",
                textDecoration: "underline",
                padding: 0,
              }}
            >
              Cambiar email
            </button>
          </form>
        )}

        {/* Messages */}
        {successMsg && (
          <div style={{ marginTop: 16, color: GREEN, fontSize: 12, background: `${GREEN}12`, border: `1px solid ${GREEN}33`, borderRadius: 8, padding: "9px 12px", textAlign: "center" }}>
            {successMsg}
          </div>
        )}
        {errorMsg && (
          <div style={{ marginTop: 16, color: RED, fontSize: 12, background: `${RED}12`, border: `1px solid ${RED}33`, borderRadius: 8, padding: "9px 12px", textAlign: "center" }}>
            {errorMsg}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Page export: wraps the form in Suspense ────────────────────────────────
// Required by Next.js App Router: any component that calls useSearchParams()
// must be wrapped in <Suspense>. Without this, Next.js throws during static
// rendering in production → blank white screen.
export default function LoginPage() {
  return (
    <Suspense
      fallback={
        <div
          style={{
            minHeight: "100vh",
            background: "#080e16",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <span style={{ color: "#6b8299", fontSize: 13 }}>Cargando…</span>
        </div>
      }
    >
      <LoginForm />
    </Suspense>
  );
}

function SubmitBtn({ label, pending }: { label: string; pending: boolean }) {
  return (
    <button
      type="submit"
      disabled={pending}
      style={{
        background: pending ? `${GREEN}66` : GREEN,
        border: "none",
        borderRadius: 10,
        color: "#000",
        fontWeight: 700,
        fontSize: 14,
        padding: "12px 0",
        cursor: pending ? "not-allowed" : "pointer",
        width: "100%",
        transition: "background 0.15s",
      }}
    >
      {pending ? "Procesando…" : label}
    </button>
  );
}
