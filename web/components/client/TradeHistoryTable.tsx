"use client";
/**
 * components/client/TradeHistoryTable.tsx
 *
 * Transparent, read-only trade history for the investor.
 * Shows gross PnL, management fee (if applied), and net PnL per allocation.
 *
 * Responsive: on narrow screens the table becomes horizontally scrollable
 * (overflow-x: auto on the wrapper). The most important columns (Symbol,
 * Net PnL) remain visible without scrolling via column order.
 */

import { useState, useMemo } from "react";
import type { TradeRow } from "./client-types";

// ─── Palette ──────────────────────────────────────────────────────────────────
const BORD  = "#1a2b3c";
const TEXT  = "#dce7f5";
const MUTE  = "#6b8299";
const GREEN = "#12d98b";
const RED   = "#eb4b61";
const BLUE  = "#57c1ff";

// ─── Helpers ─────────────────────────────────────────────────────────────────
function fmtDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString("es-ES", {
    day: "2-digit",
    month: "short",
    year: "2-digit",
    timeZone: "UTC",
  });
}

function fmtPnl(v: string, showSign = true): string {
  const n = Number(v);
  const sign = showSign ? (n > 0 ? "+" : n < 0 ? "" : "") : "";
  const abs  = Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return `${sign}$${n < 0 ? "-" : ""}${abs}`;
}

function pnlColor(v: string): string {
  const n = Number(v);
  return n > 0 ? GREEN : n < 0 ? RED : MUTE;
}

// ─── Component ────────────────────────────────────────────────────────────────
interface Props {
  trades: TradeRow[];
}

const PAGE_SIZE = 25;

export function TradeHistoryTable({ trades }: Props) {
  const [page, setPage] = useState(0);

  const totalPages = Math.ceil(trades.length / PAGE_SIZE);
  const slice      = useMemo(
    () => trades.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE),
    [trades, page]
  );

  if (trades.length === 0) {
    return (
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 10,
          padding: "60px 20px",
          color: MUTE,
        }}
      >
        <span style={{ fontSize: 32 }}>📭</span>
        <p style={{ fontSize: 13 }}>Aún no hay operaciones registradas.</p>
        <p style={{ fontSize: 11 }}>
          Las asignaciones de trades del fondo aparecerán aquí automáticamente.
        </p>
      </div>
    );
  }

  return (
    <div>
      {/* Scrollable table wrapper */}
      <div style={{ overflowX: "auto", WebkitOverflowScrolling: "touch" }}>
        <table
          style={{
            width: "100%",
            borderCollapse: "collapse",
            fontSize: 13,
            minWidth: 640,
          }}
        >
          <thead>
            <tr>
              {[
                { label: "Fecha cierre",         align: "left"  },
                { label: "Par",                  align: "left"  },
                { label: "Lado",                 align: "center"},
                { label: "Rendimiento bruto",    align: "right" },
                { label: "Comisión de gestión",  align: "right" },
                { label: "Rendimiento neto",     align: "right" },
              ].map(({ label, align }) => (
                <th
                  key={label}
                  style={{
                    color: MUTE,
                    fontSize: 10,
                    fontWeight: 600,
                    letterSpacing: "0.08em",
                    textTransform: "uppercase",
                    textAlign: align as React.CSSProperties["textAlign"],
                    paddingBottom: 10,
                    paddingRight: 16,
                    borderBottom: `1px solid ${BORD}`,
                    whiteSpace: "nowrap",
                  }}
                >
                  {label}
                </th>
              ))}
            </tr>
          </thead>

          <tbody>
            {slice.map((trade) => {
              const hasFee = Number(trade.adminFee) > 0;
              return (
                <tr
                  key={trade.id}
                  style={{ borderBottom: `1px solid rgba(26,43,60,0.5)` }}
                  onMouseEnter={(e) =>
                    ((e.currentTarget as HTMLTableRowElement).style.background =
                      "rgba(255,255,255,0.02)")
                  }
                  onMouseLeave={(e) =>
                    ((e.currentTarget as HTMLTableRowElement).style.background = "transparent")
                  }
                >
                  {/* Fecha */}
                  <td style={{ color: MUTE, padding: "11px 16px 11px 0", whiteSpace: "nowrap", fontSize: 12 }}>
                    {fmtDate(trade.closedAt)}
                  </td>

                  {/* Par */}
                  <td style={{ color: TEXT, fontWeight: 600, padding: "11px 16px 11px 0", whiteSpace: "nowrap" }}>
                    <span>{trade.symbol}</span>
                    {trade.broker && (
                      <span
                        title={`Broker: ${trade.broker}`}
                        style={{
                          marginLeft: 8,
                          padding: "1px 6px",
                          borderRadius: 4,
                          fontSize: 9,
                          fontWeight: 700,
                          letterSpacing: "0.06em",
                          textTransform: "uppercase",
                          background: trade.broker === "deriv" ? "rgba(87,193,255,0.12)" : "rgba(245,179,0,0.12)",
                          color:      trade.broker === "deriv" ? "#57c1ff" : "#f5b300",
                          border:     `1px solid ${trade.broker === "deriv" ? "rgba(87,193,255,0.4)" : "rgba(245,179,0,0.4)"}`,
                        }}
                      >
                        {trade.broker}
                      </span>
                    )}
                  </td>

                  {/* Lado */}
                  <td style={{ padding: "11px 16px 11px 0", textAlign: "center" }}>
                    <span
                      style={{
                        display: "inline-block",
                        padding: "2px 8px",
                        borderRadius: 6,
                        fontSize: 10,
                        fontWeight: 700,
                        letterSpacing: "0.06em",
                        background:
                          trade.side === "buy" ? `${GREEN}1a` : `${RED}1a`,
                        color: trade.side === "buy" ? GREEN : RED,
                        border: `1px solid ${trade.side === "buy" ? `${GREEN}44` : `${RED}44`}`,
                        textTransform: "uppercase",
                      }}
                    >
                      {trade.side === "buy" ? "▲ BUY" : "▼ SELL"}
                    </span>
                  </td>

                  {/* Rendimiento bruto */}
                  <td
                    style={{
                      color: pnlColor(trade.grossPnl),
                      fontFamily: "monospace",
                      textAlign: "right",
                      padding: "11px 16px 11px 0",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {fmtPnl(trade.grossPnl)}
                  </td>

                  {/* Comisión de gestión */}
                  <td
                    style={{
                      color: hasFee ? BLUE : MUTE,
                      fontFamily: "monospace",
                      textAlign: "right",
                      padding: "11px 16px 11px 0",
                      whiteSpace: "nowrap",
                      fontSize: hasFee ? 13 : 12,
                    }}
                  >
                    {hasFee ? `−$${Number(trade.adminFee).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "—"}
                  </td>

                  {/* Rendimiento neto */}
                  <td
                    style={{
                      color: pnlColor(trade.netPnl),
                      fontFamily: "monospace",
                      fontWeight: 700,
                      textAlign: "right",
                      padding: "11px 0",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {fmtPnl(trade.netPnl)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            paddingTop: 20,
            borderTop: `1px solid ${BORD}`,
            marginTop: 8,
          }}
        >
          <span style={{ color: MUTE, fontSize: 11 }}>
            {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, trades.length)} de {trades.length}
          </span>
          <div style={{ display: "flex", gap: 8 }}>
            <PageBtn
              label="← Anterior"
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              disabled={page === 0}
            />
            <PageBtn
              label="Siguiente →"
              onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
              disabled={page === totalPages - 1}
            />
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Pagination button ────────────────────────────────────────────────────────
function PageBtn({
  label,
  onClick,
  disabled,
}: {
  label: string;
  onClick: () => void;
  disabled: boolean;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{
        background: "rgba(255,255,255,0.04)",
        border: `1px solid ${BORD}`,
        borderRadius: 8,
        color: disabled ? MUTE : TEXT,
        fontSize: 12,
        padding: "6px 14px",
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.4 : 1,
        transition: "background 0.15s",
      }}
      onMouseEnter={(e) => {
        if (!disabled) (e.currentTarget as HTMLButtonElement).style.background = "rgba(255,255,255,0.08)";
      }}
      onMouseLeave={(e) => {
        (e.currentTarget as HTMLButtonElement).style.background = "rgba(255,255,255,0.04)";
      }}
    >
      {label}
    </button>
  );
}
