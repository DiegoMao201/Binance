"use client";
/**
 * OnboardingForm — Client Component for investor onboarding.
 *
 * Uses React 19 useActionState to bind directly to the createInvestor
 * Server Action. The form resets on success, preserving accessibility
 * and avoiding stale state.
 */

import { useActionState, useEffect, useRef } from "react";
import { createInvestor, type CreateInvestorState } from "@/app/actions/admin";

const INITIAL_STATE: CreateInvestorState = { success: false };

// ─── Colour palette (mirrors dashboard-client.js) ────────────────────────────
const CARD   = "#0a1018";
const BORD   = "#1a2b3c";
const INPUT  = "#0e1520";
const TEXT   = "#dce7f5";
const MUTE   = "#6b8299";
const GREEN  = "#12d98b";
const RED    = "#eb4b61";
const YELLOW = "#f4b942";

export function OnboardingForm() {
  const [state, formAction, isPending] = useActionState(
    createInvestor,
    INITIAL_STATE
  );
  const formRef = useRef<HTMLFormElement>(null);

  // Reset form fields after a successful creation.
  useEffect(() => {
    if (state.success) {
      formRef.current?.reset();
    }
  }, [state.success]);

  return (
    <div
      style={{
        background: CARD,
        border: `1px solid ${BORD}`,
        borderRadius: 16,
        padding: 24,
      }}
    >
      {/* Section header */}
      <p
        style={{
          color: MUTE,
          fontSize: 10,
          fontWeight: 600,
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          marginBottom: 20,
        }}
      >
        Alta de Inversor
      </p>

      <form ref={formRef} action={formAction} style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {/* ── Name ── */}
        <Field
          label="Nombre completo"
          name="name"
          type="text"
          placeholder="María García"
          error={state.fieldErrors?.name?.[0]}
          disabled={isPending}
        />

        {/* ── Email ── */}
        <Field
          label="Email del inversor"
          name="email"
          type="email"
          placeholder="inversor@empresa.com"
          error={state.fieldErrors?.email?.[0]}
          disabled={isPending}
        />

        {/* ── Fee preview card ── */}
        <div
          style={{
            background: "rgba(255,255,255,0.02)",
            border: `1px solid ${BORD}`,
            borderRadius: 10,
            padding: "10px 14px",
            display: "flex",
            flexDirection: "column",
            gap: 4,
          }}
        >
          <FeeRow label="Capital inicial" value="100 USDT fijo" color={GREEN} />
          <FeeRow label="Entry Fee" value="0%" color={MUTE} />
          <FeeRow label="Performance Fee" value="20% sobre PnL positivo" color={YELLOW} />
        </div>

        {/* ── Submit ── */}
        <button
          type="submit"
          disabled={isPending}
          style={{
            background: isPending ? `${GREEN}66` : GREEN,
            border: "none",
            borderRadius: 10,
            color: "#000",
            fontWeight: 700,
            fontSize: 13,
            padding: "11px 0",
            cursor: isPending ? "not-allowed" : "pointer",
            transition: "opacity 0.15s",
            width: "100%",
          }}
        >
          {isPending ? "Creando inversor…" : "Crear Inversor"}
        </button>

        {/* ── Status messages ── */}
        {state.success && (
          <StatusBanner color={GREEN} message="✓ Inversor creado exitosamente." />
        )}
        {!state.success && state.error && !state.fieldErrors && (
          <StatusBanner color={RED} message={state.error} />
        )}
      </form>
    </div>
  );
}

// ─── Sub-components ───────────────────────────────────────────────────────────

interface FieldProps {
  label: string;
  name: string;
  type: string;
  placeholder: string;
  error?: string;
  disabled: boolean;
  inputMode?: React.InputHTMLAttributes<HTMLInputElement>["inputMode"];
}

function Field({ label, name, type, placeholder, error, disabled, inputMode }: FieldProps) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      <label
        htmlFor={name}
        style={{ color: MUTE, fontSize: 11, fontWeight: 500 }}
      >
        {label}
      </label>
      <input
        id={name}
        name={name}
        type={type}
        inputMode={inputMode}
        placeholder={placeholder}
        disabled={disabled}
        required
        style={{
          background: INPUT,
          border: `1px solid ${error ? RED : BORD}`,
          borderRadius: 8,
          color: TEXT,
          fontSize: 13,
          padding: "9px 12px",
          outline: "none",
          width: "100%",
          boxSizing: "border-box",
          opacity: disabled ? 0.6 : 1,
        }}
      />
      {error && (
        <span style={{ color: RED, fontSize: 11 }}>{error}</span>
      )}
    </div>
  );
}

function FeeRow({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
      <span style={{ color: MUTE, fontSize: 11 }}>{label}</span>
      <span style={{ color, fontSize: 11, fontWeight: 600 }}>{value}</span>
    </div>
  );
}

function StatusBanner({ color, message }: { color: string; message: string }) {
  return (
    <div
      style={{
        background: `${color}14`,
        border: `1px solid ${color}33`,
        borderRadius: 8,
        color,
        fontSize: 12,
        padding: "9px 12px",
        textAlign: "center",
      }}
    >
      {message}
    </div>
  );
}
