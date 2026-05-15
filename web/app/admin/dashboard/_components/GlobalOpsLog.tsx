"use client";
/**
 * GlobalOpsLog.tsx — Admin Global Operations Log
 *
 * Renders every LedgerTransaction across ALL users in a paginated, scannable
 * dark-mode table. Amounts are displayed to 8 decimal places for full auditability.
 *
 * Props are plain serialisable types (string, number) — safe to pass from a
 * Server Component.
 */

import { useState, useMemo } from "react";

// ─── Palette ──────────────────────────────────────────────────────────────────
const BORD  = "rgba(63,87,114,0.28)";
const TEXT  = "#dce7f5";
const MUTE  = "#6b8299";
const GREEN = "#34d399";
const RED   = "#fb7185";
const BLUE  = "#60a5fa";
const PURP  = "#c084fc";

// ─── Public type ─────────────────────────────────────────────────────────────
/** One ledger row as serialised strings (Server → Client safe). */
export type LedgerRow = {
  /** BigInt ledger ID — serialised as string. */
  id: string;
  /** UUID of the user who owns this entry. */
  userId: string;
  /** Short display alias: email or name. */
  userAlias: string;
  /** Ledger transaction type. */
  type: string;
  /**
   * Amount in USDT. Always positive; direction is implied by `type`.
   * Serialised to 8 decimal places (e.g. "0.00012300").
   */
  amount: string;
  /** Optional human-readable description set at write time. */
  description: string | null;
  /** ISO 8601 timestamp — e.g. "2026-05-15T10:23:45.000Z". */
  createdAt: string;
};

interface Props {
  rows: LedgerRow[];
}

// ─── Colour per ledger type ───────────────────────────────────────────────────
type BadgeStyle = { color: string; bg: string; border: string };

function typeBadge(type: string): BadgeStyle {
  switch (type) {
    case "DEPOSIT":                    return { color: "#60a5fa",  bg: "rgba(23,37,84,0.5)",   border: "rgba(59,130,246,0.3)" };
    case "WITHDRAWAL":                 return { color: "#fb7185",  bg: "rgba(69,10,10,0.5)",   border: "rgba(239,68,68,0.3)" };
    case "TRADE_PNL":                  return { color: "#34d399",  bg: "rgba(2,44,34,0.5)",    border: "rgba(16,185,129,0.3)" };
    case "PERFORMANCE_FEE":            return { color: "#c084fc",  bg: "rgba(46,16,101,0.5)",  border: "rgba(168,85,247,0.3)" };
    case "BINANCE_FEE_REIMBURSEMENT":
    case "BINANCE_COMMISSION":         return { color: "#fbbf24",  bg: "rgba(69,26,3,0.5)",    border: "rgba(245,158,11,0.3)" };
    case "ENTRY_FEE":                  return { color: "#fbbf24",  bg: "rgba(69,26,3,0.5)",    border: "rgba(245,158,11,0.3)" };
    default:                           return { color: MUTE,        bg: "rgba(30,40,50,0.5)",   border: BORD };
  }
}

function typeColor(type: string): string { return typeBadge(type).color; }

// ─── Short type label ─────────────────────────────────────────────────────────
function typeLabel(type: string): string {
  const map: Record<string, string> = {
    DEPOSIT:                   "DEPOSIT",
    WITHDRAWAL:                "WITHDRAW",
    TRADE_PNL:                 "TRADE PNL",
    PERFORMANCE_FEE:           "PERF FEE",
    BINANCE_FEE_REIMBURSEMENT: "BNB FEE",
    BINANCE_COMMISSION:        "BNB FEE",
    ENTRY_FEE:                 "ENTRY FEE",
  };
  return map[type] ?? type;
}

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleString("es-ES", {
    day:    "2-digit",
    month:  "short",
    year:   "2-digit",
    hour:   "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
  });
}

// ─── Pagination ───────────────────────────────────────────────────────────────
const PAGE_SIZE = 50;

// ─── Component ────────────────────────────────────────────────────────────────
export function GlobalOpsLog({ rows }: Props) {
  const [page, setPage]   = useState(0);
  const [filter, setFilter] = useState("");

  const filtered = useMemo(() => {
    if (!filter.trim()) return rows;
    const q = filter.toLowerCase();
    return rows.filter(
      (r) =>
        r.userAlias.toLowerCase().includes(q) ||
        r.type.toLowerCase().includes(q) ||
        (r.description ?? "").toLowerCase().includes(q) ||
        r.userId.toLowerCase().includes(q),
    );
  }, [rows, filter]);

  const totalPages = Math.ceil(filtered.length / PAGE_SIZE);
  const slice = useMemo(
    () => filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE),
    [filtered, page],
  );

  const handleFilterChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setFilter(e.target.value);
    setPage(0); // reset page on new search
  };

  return (
    <div>
      {/* ── Header + search ── */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 12,
          marginBottom: 16,
          flexWrap: "wrap",
        }}
      >
        <div>
          <p
            style={{
              color: MUTE,
              fontSize: 10,
              fontWeight: 600,
              letterSpacing: "0.12em",
              textTransform: "uppercase",
              marginBottom: 4,
            }}
          >
            Global Operations Log
          </p>
          <p style={{ color: TEXT, fontSize: 13 }}>
            {rows.length.toLocaleString()} entries total
          </p>
        </div>

        {/* Search / filter input */}
        <input
          type="text"
          value={filter}
          onChange={handleFilterChange}
          placeholder="Filtrar por usuario, tipo o descripción…"
          style={{
            background: "rgba(4,7,12,0.85)",
            backdropFilter: "blur(8px)",
            border: `1px solid ${BORD}`,
            borderRadius: 10,
            padding: "8px 14px",
            color: TEXT,
            fontSize: 12,
            outline: "none",
            width: 280,
            fontFamily: "ui-monospace, Menlo, monospace",
          }}
        />
      </div>

      {/* ── Table ── */}
      <div style={{ overflowX: "auto" }}>
        <table
          style={{
            width: "100%",
            borderCollapse: "collapse",
            fontSize: 12,
            tableLayout: "fixed",
          }}
        >
          <colgroup>
            <col style={{ width: 90 }} />   {/* Date */}
            <col style={{ width: 160 }} />  {/* User */}
            <col style={{ width: 120 }} />  {/* Type */}
            <col style={{ width: 160 }} />  {/* Amount */}
            <col />                          {/* Description */}
          </colgroup>
          <thead>
            <tr style={{ borderBottom: `1px solid ${BORD}` }}>
              {["Fecha (UTC)", "Usuario", "Tipo", "Monto (USDT)", "Descripción"].map((h) => (
                <th
                  key={h}
                  style={{
                    padding: "8px 12px",
                    textAlign: "left",
                    color: MUTE,
                    fontWeight: 600,
                    letterSpacing: "0.08em",
                    textTransform: "uppercase",
                    fontSize: 10,
                    whiteSpace: "nowrap",
                  }}
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {slice.length === 0 ? (
              <tr>
                <td
                  colSpan={5}
                  style={{
                    padding: "40px 12px",
                    textAlign: "center",
                    color: MUTE,
                  }}
                >
                  No hay registros que coincidan con la búsqueda.
                </td>
              </tr>
            ) : (
              slice.map((row) => (
                <tr
                  key={row.id}
                  style={{
                    borderBottom: `1px solid ${BORD}22`,
                    transition: "background 0.15s",
                  }}
                  onMouseEnter={(e) =>
                    ((e.currentTarget as HTMLTableRowElement).style.background = "rgba(34,211,238,0.05)")
                  }
                  onMouseLeave={(e) =>
                    ((e.currentTarget as HTMLTableRowElement).style.background = "transparent")
                  }
                >
                  {/* Date */}
                  <td style={{ padding: "10px 12px", color: MUTE, whiteSpace: "nowrap" }}>
                    {fmtDate(row.createdAt)}
                  </td>

                  {/* User alias + truncated UUID */}
                  <td style={{ padding: "10px 12px" }}>
                    <span style={{ color: TEXT, fontWeight: 600 }}>
                      {row.userAlias}
                    </span>
                    <br />
                    <span
                      style={{
                        color: MUTE,
                        fontSize: 10,
                        fontFamily: "monospace",
                      }}
                    >
                      {row.userId.slice(0, 8)}…
                    </span>
                  </td>

                  {/* Type badge — exact design tokens */}
                  <td style={{ padding: "10px 12px" }}>
                    {(() => {
                      const b = typeBadge(row.type);
                      return (
                        <span
                          style={{
                            display: "inline-block",
                            background: b.bg,
                            border: `1px solid ${b.border}`,
                            color: b.color,
                            borderRadius: 999,
                            padding: "3px 10px",
                            fontSize: 10,
                            fontWeight: 700,
                            letterSpacing: "0.08em",
                            whiteSpace: "nowrap",
                            fontFamily: "ui-monospace, Menlo, monospace",
                          }}
                        >
                          {typeLabel(row.type)}
                        </span>
                      );
                    })()}
                  </td>

                  {/* Amount — 8 decimal places for full auditability */}
                  <td
                    style={{
                      padding: "10px 12px",
                      fontFamily: "ui-monospace, Menlo, monospace",
                      fontWeight: 700,
                      color: typeColor(row.type),
                      whiteSpace: "nowrap",
                    }}
                  >
                    {Number(row.amount).toFixed(8)}
                    <span style={{ color: MUTE, fontWeight: 400, marginLeft: 4, fontSize: 10 }}>
                      USDT
                    </span>
                  </td>

                  {/* Description */}
                  <td
                    style={{
                      padding: "10px 12px",
                      color: MUTE,
                      fontSize: 11,
                      maxWidth: 0,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                    title={row.description ?? ""}
                  >
                    {row.description ?? "—"}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* ── Pagination ── */}
      {totalPages > 1 && (
        <div
          style={{
            display: "flex",
            justifyContent: "center",
            alignItems: "center",
            gap: 10,
            marginTop: 16,
          }}
        >
          <button
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={page === 0}
            style={{
              background: "transparent",
              border: `1px solid ${BORD}`,
              color: page === 0 ? MUTE : TEXT,
              borderRadius: 8,
              padding: "6px 14px",
              cursor: page === 0 ? "not-allowed" : "pointer",
              fontSize: 12,
            }}
          >
            ← Prev
          </button>
          <span style={{ color: MUTE, fontSize: 12 }}>
            {page + 1} / {totalPages}
          </span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
            disabled={page === totalPages - 1}
            style={{
              background: "transparent",
              border: `1px solid ${BORD}`,
              color: page === totalPages - 1 ? MUTE : TEXT,
              borderRadius: 8,
              padding: "6px 14px",
              cursor: page === totalPages - 1 ? "not-allowed" : "pointer",
              fontSize: 12,
            }}
          >
            Next →
          </button>
        </div>
      )}
    </div>
  );
}
