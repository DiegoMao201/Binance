"use client";
/**
 * TreasuryModal — Capital operation modal (Add / Withdraw).
 *
 * Used by InvestorTable. Calls addCapital / withdrawCapital Server Actions
 * via useTransition for loading-state feedback. Renders an amount preview
 * (entry fee breakdown for add, or balance check for withdraw).
 */

import { useTransition, useState } from "react";
import { useRouter } from "next/navigation";
import { addCapital, withdrawCapital } from "@/app/actions/treasury";

// ─── Colour palette ────────────────────────────────────────────────────────────
const BG      = "#080e16";
const CARD    = "#0a1018";
const BORD    = "#1a2b3c";
const INPUT   = "#0e1520";
const TEXT    = "#dce7f5";
const MUTE    = "#6b8299";
const GREEN   = "#12d98b";
const RED     = "#eb4b61";
const AMBER   = "#f4b942";
const INDIGO  = "#6366f1";

// ─── Types ─────────────────────────────────────────────────────────────────────

export type ModalAction = "add" | "withdraw";

interface Props {
  userId:          string;
  userName:        string;
  currentBalance:  string;   // serialised Decimal string
  action:          ModalAction;
  onClose:         () => void;
}

// ─── Helpers ───────────────────────────────────────────────────────────────────

function fmt(v: number): string {
  return "$" + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// ─── Component ─────────────────────────────────────────────────────────────────

export function TreasuryModal({ userId, userName, currentBalance, action, onClose }: Props) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [rawAmount, setRawAmount] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const balance    = parseFloat(currentBalance) || 0;
  const amount     = parseFloat(rawAmount)       || 0;
  const isAdd      = action === "add";

  // Preview values
  const entryFee   = isAdd ? +(amount * 0.02).toFixed(2) : 0;
  const netAmount  = isAdd ? +(amount * 0.98).toFixed(2) : amount;
  const newBalance = isAdd ? balance + netAmount : balance - amount;
  const insufficient = !isAdd && amount > 0 && amount > balance;

  const canSubmit = amount > 0 && !insufficient && !isPending && !done;

  function handleSubmit() {
    setError(null);
    startTransition(async () => {
      const result = isAdd
        ? await addCapital(userId, rawAmount)
        : await withdrawCapital(userId, rawAmount);

      if (result.success) {
        setDone(true);
        router.refresh();
        setTimeout(onClose, 1200);
      } else {
        setError(result.error ?? "Error inesperado.");
      }
    });
  }

  return (
    /* Backdrop */
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0,
        background: "rgba(0,0,0,0.72)",
        backdropFilter: "blur(4px)",
        display: "flex", alignItems: "center", justifyContent: "center",
        zIndex: 1000,
      }}
    >
      {/* Panel */}
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: CARD,
          border: `1px solid ${BORD}`,
          borderRadius: 16,
          padding: "28px 32px",
          width: 420,
          maxWidth: "92vw",
          boxSizing: "border-box",
        }}
      >
        {/* Header */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 24 }}>
          <div>
            <p style={{ margin: 0, color: MUTE, fontSize: 10, fontWeight: 600, letterSpacing: "0.12em", textTransform: "uppercase", marginBottom: 4 }}>
              {isAdd ? "Aportar capital" : "Retirar capital"}
            </p>
            <p style={{ margin: 0, color: TEXT, fontWeight: 700, fontSize: 16 }}>
              {userName}
            </p>
          </div>
          <button
            onClick={onClose}
            style={{
              background: "none", border: "none", cursor: "pointer",
              color: MUTE, fontSize: 20, lineHeight: 1, padding: 4,
            }}
          >
            ×
          </button>
        </div>

        {/* Current balance pill */}
        <div style={{ background: INPUT, borderRadius: 8, padding: "10px 14px", marginBottom: 20, display: "flex", justifyContent: "space-between" }}>
          <span style={{ color: MUTE, fontSize: 12 }}>Balance actual</span>
          <span style={{ color: TEXT, fontFamily: "monospace", fontWeight: 600, fontSize: 13 }}>{fmt(balance)}</span>
        </div>

        {/* Amount input */}
        <label style={{ display: "block", marginBottom: 6, color: MUTE, fontSize: 11, fontWeight: 600, letterSpacing: "0.08em", textTransform: "uppercase" }}>
          Monto (USDT)
        </label>
        <input
          type="number"
          min="0.01"
          step="0.01"
          placeholder="0.00"
          value={rawAmount}
          onChange={(e) => { setRawAmount(e.target.value); setError(null); }}
          disabled={isPending || done}
          style={{
            width: "100%", boxSizing: "border-box",
            background: INPUT, border: `1px solid ${insufficient ? RED : BORD}`,
            borderRadius: 8, color: TEXT,
            padding: "12px 14px", fontSize: 16,
            fontFamily: "monospace", fontWeight: 600,
            outline: "none", marginBottom: 16,
          }}
        />

        {/* Preview breakdown */}
        {amount > 0 && (
          <div style={{ background: BG, border: `1px solid ${BORD}`, borderRadius: 10, padding: "14px 16px", marginBottom: 16 }}>
            {isAdd ? (
              <>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                  <span style={{ color: MUTE, fontSize: 12 }}>Importe bruto</span>
                  <span style={{ color: TEXT, fontSize: 12, fontFamily: "monospace" }}>{fmt(amount)}</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                  <span style={{ color: AMBER, fontSize: 12 }}>Entry fee (2%)</span>
                  <span style={{ color: AMBER, fontSize: 12, fontFamily: "monospace" }}>− {fmt(entryFee)}</span>
                </div>
                <div style={{ borderTop: `1px solid ${BORD}`, paddingTop: 8, display: "flex", justifyContent: "space-between" }}>
                  <span style={{ color: GREEN, fontSize: 12, fontWeight: 700 }}>Capital neto acreditado</span>
                  <span style={{ color: GREEN, fontSize: 13, fontFamily: "monospace", fontWeight: 700 }}>{fmt(netAmount)}</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", marginTop: 8 }}>
                  <span style={{ color: MUTE, fontSize: 12 }}>Nuevo balance</span>
                  <span style={{ color: TEXT, fontSize: 12, fontFamily: "monospace" }}>{fmt(newBalance)}</span>
                </div>
              </>
            ) : (
              <>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                  <span style={{ color: MUTE, fontSize: 12 }}>Retiro</span>
                  <span style={{ color: TEXT, fontSize: 12, fontFamily: "monospace" }}>− {fmt(amount)}</span>
                </div>
                <div style={{ borderTop: `1px solid ${BORD}`, paddingTop: 8, display: "flex", justifyContent: "space-between" }}>
                  <span style={{ color: insufficient ? RED : MUTE, fontSize: 12, fontWeight: 700 }}>Nuevo balance</span>
                  <span style={{ color: insufficient ? RED : TEXT, fontSize: 13, fontFamily: "monospace", fontWeight: 700 }}>{fmt(newBalance)}</span>
                </div>
                {insufficient && (
                  <p style={{ margin: "8px 0 0", color: RED, fontSize: 11 }}>
                    Balance insuficiente para este retiro.
                  </p>
                )}
              </>
            )}
          </div>
        )}

        {/* Error message */}
        {error && (
          <p style={{ margin: "0 0 14px", color: RED, fontSize: 12, background: "rgba(235,75,97,0.08)", borderRadius: 8, padding: "8px 12px" }}>
            {error}
          </p>
        )}

        {/* Success state */}
        {done && (
          <p style={{ margin: "0 0 14px", color: GREEN, fontSize: 13, textAlign: "center", fontWeight: 600 }}>
            ✓ {isAdd ? "Aporte registrado" : "Retiro registrado"} correctamente.
          </p>
        )}

        {/* Actions */}
        <div style={{ display: "flex", gap: 10 }}>
          <button
            onClick={onClose}
            disabled={isPending}
            style={{
              flex: 1, background: INPUT, border: `1px solid ${BORD}`,
              borderRadius: 8, color: MUTE,
              padding: "11px 0", cursor: "pointer", fontSize: 13, fontWeight: 600,
            }}
          >
            Cancelar
          </button>
          <button
            onClick={handleSubmit}
            disabled={!canSubmit}
            style={{
              flex: 2, borderRadius: 8,
              background: canSubmit ? (isAdd ? GREEN : INDIGO) : "rgba(255,255,255,0.05)",
              border: "none",
              color: canSubmit ? (isAdd ? "#080e16" : "#fff") : MUTE,
              padding: "11px 0", cursor: canSubmit ? "pointer" : "not-allowed",
              fontSize: 13, fontWeight: 700, transition: "opacity 0.15s",
            }}
          >
            {isPending ? "Procesando…" : isAdd ? "Confirmar aporte" : "Confirmar retiro"}
          </button>
        </div>
      </div>
    </div>
  );
}
