/**
 * app/client/dashboard/page.tsx — Investor Client Dashboard (v2 simple-commission)
 *
 * Server Component que solo:
 *  - Verifica JWT y role.
 *  - Garantiza que el cliente tenga capital_inicial + fecha_inicio.
 *  - Monta el view client-side que hace polling a las APIs.
 *
 * Toda la logica de UI vive en _components/ClientDashboardView.tsx para
 * permitir refrescos en tiempo real (5s balance/posiciones, 30s stats).
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { verifyJWT } from "@/lib/auth";
import { getClientProfile } from "@/lib/clientData";
import { ClientDashboardView } from "./_components/ClientDashboardView";

export const dynamic = "force-dynamic";

export default async function ClientDashboardPage() {
  const cookieStore = await cookies();
  const token = cookieStore.get("auth_token")?.value;
  const payload = token ? await verifyJWT(token) : null;

  if (!payload) {
    redirect("/portal/login?next=/client/dashboard");
  }
  if (payload.role === "admin") {
    redirect("/admin/dashboard");
  }
  if (payload.role !== "client" && payload.role !== "investor") {
    redirect("/portal/login?next=/client/dashboard");
  }

  const profile = await getClientProfile(payload.sub);
  if (!profile) {
    redirect("/portal/login");
  }

  // Si la cuenta no tiene capital_inicial / fecha_inicio, renderizamos un
  // mensaje claro en lugar de exponer un dashboard incoherente.
  if (!profile.capitalInicial || !profile.fechaInicio) {
    return (
      <div style={{
        minHeight: "100vh",
        background: "#04070c",
        color: "#dce7f5",
        padding: 40,
        fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
      }}>
        <h1 style={{ fontSize: 22 }}>Cuenta pendiente de configuracion</h1>
        <p style={{ color: "#6b8299", maxWidth: 520, lineHeight: 1.5 }}>
          Tu cuenta esta creada pero todavia no tiene <code>capital_inicial</code> o{" "}
          <code>fecha_inicio</code> asignados. Avisa al administrador para que termine
          la configuracion antes de operar.
        </p>
      </div>
    );
  }

  return <ClientDashboardView />;
}
