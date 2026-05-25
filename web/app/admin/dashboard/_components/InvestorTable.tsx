"use client";
/**
 * InvestorTable — Client Component for the investor data grid.
 *
 * Receives pre-serialised InvestorRow[] from the Server Component.
 * All monetary values are strings — rendered with toLocaleString() for
 * locale-appropriate thousands separators.
 *
 * Each row has "Aportar" / "Retirar" action buttons that open TreasuryModal.
 */

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import type { InvestorRow } from "@/lib/pamm";
import { TreasuryModal, type ModalAction } from "./TreasuryModal";
import { deactivateInvestor, setInvestorBalance } from "@/app/actions/admin";

const CARD   = "#0a1018";
const BORD   = "#1a2b3c";
const TEXT   = "#dce7f5";
const MUTE   = "#6b8299";
const GREEN  = "#12d98b";
const RED    = "#eb4b61";
const INDIGO = "#6366f1";

function fmtUSDT(v: string): string {
  return "$" + Number(v).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

interface Props {
  investors: InvestorRow[];
}

type ActiveModal = {
  userId:         string;
  userName:       string;
  currentBalance: string;
  action:         ModalAction;
};

export function InvestorTable({ investors }: Props) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [activeModal, setActiveModal] = useState<ActiveModal | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [processingUserId, setProcessingUserId] = useState<string | null>(null);

  function openModal(inv: InvestorRow, action: ModalAction) {
    setActiveModal({
      userId:         inv.id,
      userName:       inv.name,
      currentBalance: inv.balance,
      action,
    });
  }

  function handleSetBalance(inv: InvestorRow) {
    const nextBalance = window.prompt(
      `Nuevo balance para ${inv.name} (USDT):`,
      inv.balance,
    );
    if (nextBalance === null) {
      return;
    }

    setActionError(null);
    setProcessingUserId(inv.id);
    startTransition(async () => {
      const result = await setInvestorBalance(inv.id, nextBalance);
      if (!result.success) {
        setActionError(result.error ?? "No se pudo actualizar el balance.");
      } else {
        router.refresh();
      }
      setProcessingUserId(null);
    });
  }

  function handleDeactivate(inv: InvestorRow) {
    const confirmed = window.confirm(
      `Se desactivara la cuenta de ${inv.name} y su balance pasara a 0. ¿Continuar?`,
    );
    if (!confirmed) {
      return;
    }

    setActionError(null);
    setProcessingUserId(inv.id);
    startTransition(async () => {
      const result = await deactivateInvestor(inv.id);
      if (!result.success) {
        setActionError(result.error ?? "No se pudo desactivar la cuenta.");
      } else {
        router.refresh();
      }
      setProcessingUserId(null);
    });
  }

  return (
    <>
      {activeModal && (
        <TreasuryModal
          {...activeModal}
          onClose={() => setActiveModal(null)}
        />
      )}
    <div
      style={{
        background: CARD,
        border: `1px solid ${BORD}`,
        borderRadius: 16,
        padding: 24,
        height: "100%",
        boxSizing: "border-box",
      }}
    >
      {/* Header */}
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
        Inversores{" "}
        <span
          style={{
            color: TEXT,
            background: "rgba(255,255,255,0.06)",
            borderRadius: 20,
            padding: "1px 8px",
            fontSize: 11,
            marginLeft: 4,
          }}
        >
          {investors.length}
        </span>
      </p>

      {investors.length === 0 ? (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            height: 140,
            color: MUTE,
            fontSize: 13,
          }}
        >
          Aún no hay inversores registrados.
        </div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table
            style={{
              width: "100%",
              borderCollapse: "collapse",
              fontSize: 13,
            }}
          >
            <thead>
              <tr>
                {["Nombre", "Email", "Depósito bruto", "Balance actual", "ROI neto", "Acciones"].map(
                  (col) => (
                    <th
                      key={col}
                      style={{
                        color: MUTE,
                        fontSize: 10,
                        fontWeight: 600,
                        letterSpacing: "0.08em",
                        textTransform: "uppercase",
                        textAlign: col === "Nombre" || col === "Email" || col === "Acciones" ? "left" : "right",
                        paddingBottom: 10,
                        borderBottom: `1px solid ${BORD}`,
                        whiteSpace: "nowrap",
                        paddingRight: col !== "Acciones" ? 16 : 0,
                      }}
                    >
                      {col}
                    </th>
                  )
                )}
              </tr>
            </thead>
            <tbody>
              {investors.map((inv) => {
                const roi = parseFloat(inv.roi);
                const positive = roi >= 0;
                  const rowBusy = isPending && processingUserId === inv.id;
                return (
                  <tr
                    key={inv.id}
                    style={{
                      borderBottom: `1px solid rgba(26,43,60,0.5)`,
                      transition: "background 0.1s",
                    }}
                    onMouseEnter={(e) =>
                      ((e.currentTarget as HTMLTableRowElement).style.background =
                        "rgba(255,255,255,0.02)")
                    }
                    onMouseLeave={(e) =>
                      ((e.currentTarget as HTMLTableRowElement).style.background =
                        "transparent")
                    }
                  >
                    <td
                      style={{
                        color: TEXT,
                        fontWeight: 600,
                        padding: "11px 16px 11px 0",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {inv.name}
                    </td>
                    <td
                      style={{
                        color: MUTE,
                        padding: "11px 16px 11px 0",
                        fontSize: 12,
                      }}
                    >
                      {inv.email}
                    </td>
                    <td
                      style={{
                        color: MUTE,
                        fontFamily: "monospace",
                        textAlign: "right",
                        padding: "11px 16px 11px 0",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {fmtUSDT(inv.grossDeposit)}
                    </td>
                    <td
                      style={{
                        color: TEXT,
                        fontFamily: "monospace",
                        fontWeight: 600,
                        textAlign: "right",
                        padding: "11px 16px 11px 0",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {fmtUSDT(inv.balance)}
                    </td>
                    <td
                      style={{
                        color: positive ? GREEN : RED,
                        fontFamily: "monospace",
                        fontWeight: 700,
                        textAlign: "right",
                        padding: "11px 16px 11px 0",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {positive ? "+" : ""}
                      {roi.toFixed(2)}%
                    </td>
                    <td style={{ padding: "8px 0", whiteSpace: "nowrap" }}>
                      <button
                        onClick={() => openModal(inv, "add")}
                        disabled={rowBusy}
                        title="Aportar capital"
                        style={{
                          background: "rgba(18,217,139,0.1)",
                          border: `1px solid rgba(18,217,139,0.3)`,
                          borderRadius: 6,
                          color: GREEN,
                          fontSize: 11,
                          fontWeight: 700,
                          padding: "5px 10px",
                          cursor: "pointer",
                          marginRight: 6,
                          transition: "background 0.15s",
                          opacity: rowBusy ? 0.5 : 1,
                        }}
                        onMouseEnter={(e) => ((e.currentTarget as HTMLButtonElement).style.background = "rgba(18,217,139,0.2)")}
                        onMouseLeave={(e) => ((e.currentTarget as HTMLButtonElement).style.background = "rgba(18,217,139,0.1)")}
                      >
                        + Aportar
                      </button>
                      <button
                        onClick={() => openModal(inv, "withdraw")}
                        disabled={rowBusy}
                        title="Retirar capital"
                        style={{
                          background: "rgba(99,102,241,0.1)",
                          border: `1px solid rgba(99,102,241,0.3)`,
                          borderRadius: 6,
                          color: INDIGO,
                          fontSize: 11,
                          fontWeight: 700,
                          padding: "5px 10px",
                          cursor: "pointer",
                          transition: "background 0.15s",
                          opacity: rowBusy ? 0.5 : 1,
                        }}
                        onMouseEnter={(e) => ((e.currentTarget as HTMLButtonElement).style.background = "rgba(99,102,241,0.2)")}
                        onMouseLeave={(e) => ((e.currentTarget as HTMLButtonElement).style.background = "rgba(99,102,241,0.1)")}
                      >
                        − Retirar
                      </button>
                      <button
                        onClick={() => handleSetBalance(inv)}
                        disabled={rowBusy}
                        title="Editar balance exacto"
                        style={{
                          background: "rgba(87,193,255,0.1)",
                          border: "1px solid rgba(87,193,255,0.35)",
                          borderRadius: 6,
                          color: "#57c1ff",
                          fontSize: 11,
                          fontWeight: 700,
                          padding: "5px 10px",
                          cursor: "pointer",
                          marginLeft: 6,
                          opacity: rowBusy ? 0.5 : 1,
                        }}
                      >
                        Editar saldo
                      </button>
                      <button
                        onClick={() => handleDeactivate(inv)}
                        disabled={rowBusy}
                        title="Desactivar cuenta"
                        style={{
                          background: "rgba(235,75,97,0.12)",
                          border: "1px solid rgba(235,75,97,0.35)",
                          borderRadius: 6,
                          color: RED,
                          fontSize: 11,
                          fontWeight: 700,
                          padding: "5px 10px",
                          cursor: "pointer",
                          marginLeft: 6,
                          opacity: rowBusy ? 0.5 : 1,
                        }}
                      >
                        Eliminar
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {actionError && (
        <div
          style={{
            marginTop: 12,
            background: "rgba(235,75,97,0.08)",
            border: `1px solid rgba(235,75,97,0.30)`,
            borderRadius: 8,
            color: RED,
            padding: "9px 12px",
            fontSize: 12,
          }}
        >
          {actionError}
        </div>
      )}
    </div>
    </>
  );
}
