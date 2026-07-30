"use client";
/**
 * app/error.tsx — Route-level error boundary for the root segment.
 *
 * Next.js App Router rules:
 *   • "use client" required
 *   • Do NOT include <html>/<body> here — that is only for global-error.tsx
 *   • Props: { error: Error & { digest?: string }, reset: () => void }
 */

import { useEffect } from "react";

const RED  = "#eb4b61";
const BG   = "#080e16";
const CARD = "#0a1018";
const BORD = "#1a2b3c";
const TEXT = "#dce7f5";
const MUTE = "#6b8299";

export default function RootError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("[RootError boundary] digest:", error.digest, error);
    // ChunkLoadError = deploy nuevo con chunks invalidados → hard reload automático
    const isChunk =
      error.name === "ChunkLoadError" ||
      /loading chunk|failed to fetch|loading css chunk/i.test(error.message);
    if (isChunk) {
      window.location.reload();
    }
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
        <div
          style={{
            width: 48, height: 48,
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
          Algo salió mal
        </h1>
        <p style={{ color: MUTE, fontSize: 12, lineHeight: 1.6, margin: "0 0 24px", wordBreak: "break-all" }}>
          {error.message || "Error inesperado."}
          {error.digest && (
            <span style={{ display: "block", marginTop: 8, color: `${MUTE}88`, fontSize: 10 }}>
              Ref: {error.digest}
            </span>
          )}
        </p>
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <button
            onClick={reset}
            style={{
              background: RED, border: "none", borderRadius: 10,
              color: "#fff", fontWeight: 700, fontSize: 13,
              padding: "11px 0", cursor: "pointer", width: "100%",
            }}
          >
            Reintentar
          </button>
          <button
            onClick={() => { window.location.href = "/"; }}
            style={{
              background: "rgba(255,255,255,0.04)",
              border: `1px solid ${BORD}`,
              borderRadius: 10, color: MUTE, fontSize: 13,
              padding: "10px 0", cursor: "pointer", width: "100%",
            }}
          >
            Ir al inicio
          </button>
        </div>
      </div>
    </div>
  );
}
