"use client";
/**
 * app/error.tsx — Global Next.js App Router error boundary.
 *
 * Catches unhandled exceptions that escape any page or layout.
 * Without this file, Next.js shows a blank white screen in production.
 *
 * Contract (Next.js):
 *   • Must be a Client Component ("use client")
 *   • Props: { error: Error & { digest?: string }, reset: () => void }
 *   • `error.message` is available in development; in production Next.js
 *     redacts it to "An error occurred in the Server Components render."
 *     — the `digest` can be used to correlate with server logs.
 */

import { useEffect } from "react";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  // Log to console so it appears in Coolify's container logs.
  useEffect(() => {
    console.error("[GlobalError boundary]", error);
  }, [error]);

  return (
    <html lang="es">
      <body
        style={{
          margin: 0,
          minHeight: "100vh",
          background: "#080e16",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace",
          padding: 20,
          boxSizing: "border-box",
        }}
      >
        <ErrorCard error={error} reset={reset} title="Error del sistema" />
      </body>
    </html>
  );
}
