"use client";
/**
 * InvestorTable — Client Component for the investor data grid.
 *
 * Receives pre-serialised InvestorRow[] from the Server Component.
 * All monetary values are strings — rendered with toLocaleString() for
 * locale-appropriate thousands separators.
 */

import type { InvestorRow } from "@/lib/pamm";

const CARD  = "#0a1018";
const BORD  = "#1a2b3c";
const TEXT  = "#dce7f5";
const MUTE  = "#6b8299";
const GREEN = "#12d98b";
const RED   = "#eb4b61";

function fmtUSDT(v: string): string {
  return "$" + Number(v).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

interface Props {
  investors: InvestorRow[];
}

export function InvestorTable({ investors }: Props) {
  return (
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
                {["Nombre", "Email", "Depósito bruto", "Balance actual", "ROI neto"].map(
                  (col) => (
                    <th
                      key={col}
                      style={{
                        color: MUTE,
                        fontSize: 10,
                        fontWeight: 600,
                        letterSpacing: "0.08em",
                        textTransform: "uppercase",
                        textAlign: col === "Nombre" || col === "Email" ? "left" : "right",
                        paddingBottom: 10,
                        borderBottom: `1px solid ${BORD}`,
                        whiteSpace: "nowrap",
                        paddingRight: col !== "ROI neto" ? 16 : 0,
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
                        padding: "11px 0",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {positive ? "+" : ""}
                      {roi.toFixed(2)}%
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
