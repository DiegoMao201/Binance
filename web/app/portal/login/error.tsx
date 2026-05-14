"use client";
/**
 * app/portal/login/error.tsx — Route-scoped error boundary for /portal/login.
 *
 * This file is loaded INSTEAD of the global error.tsx when an exception
 * is thrown inside the /portal/login subtree (page.tsx, layout.tsx, etc.).
 * It keeps the dark portal theme and avoids leaking stack traces.
 *
 * In production, error.message is redacted by Next.js — use error.digest
 * to look up the full trace in Coolify server logs.
 */

"use client";

import { useEffect } from "react";

// ─── Palette ──────────────────────────────────────────────────────────────────
const BG   = "#080e16";
const CARD = "#0a1018";
const BORD = "#1a2b3c";
const TEXT = "#dce7f5";
const MUTE = "#6b8299";
const RED  = "#eb4b61";

export default function LoginError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("[LoginError boundary] digest:", error.digest, error);
  }, [error]);

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
          borderLeft: `3px solid ${RED}`,
          borderRadius: 20,
          padding: "36px 32px",
          width: "100%",
          maxWidth: 420,
          textAlign: "center",
        }}
      >
        {/* Icon */}
        <div
          style={{
            width: 48,
            height: 48,
            background: `${RED}1a`,
            border: `1px solid ${RED}44`,
            borderRadius: 14,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            margin: "0 auto 20px",
            fontSize: 22,
          }}
        >
          ⚠
        </div>

        <h1 style={{ color: TEXT, fontSize: 16, fontWeight: 700, margin: "0 0 8px" }}>
          Error al cargar el login
        </h1>

        {/* Show digest in production, full message in dev */}
        <p
          style={{
            color: MUTE,
            fontSize: 12,
            lineHeight: 1.6,
            margin: "0 0 24px",
            wordBreak: "break-all",
          }}
        >
          {error.message || "Se produjo un error inesperado."}
          {error.digest && (
            <span style={{ display: "block", marginTop: 8, color: `${MUTE}88`, fontSize: 10 }}>
              Ref: {error.digest}
            </span>
          )}
        </p>

        {/* Actions */}
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <button
            onClick={reset}
            style={{
              background: RED,
              border: "none",
              borderRadius: 10,
              color: "#fff",
              fontWeight: 700,
              fontSize: 13,
              padding: "11px 0",
              cursor: "pointer",
              width: "100%",
            }}
          >
            Reintentar
          </button>
          <button
            onClick={() => { window.location.href = "/portal/login"; }}
            style={{
              background: "rgba(255,255,255,0.04)",
              border: `1px solid ${BORD}`,
              borderRadius: 10,
              color: MUTE,
              fontSize: 13,
              padding: "10px 0",
              cursor: "pointer",
              width: "100%",
            }}
          >
            Recargar página
          </button>
        </div>
      </div>
    </div>
  );
}
