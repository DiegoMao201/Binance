/**
 * app/admin/dashboard/page.tsx — Admin Backoffice (v2 simple-commission)
 *
 * Server Component minimal: solo verifica role=admin y monta el view
 * client-side. Toda la data viene de /api/admin/* via polling.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { verifyJWT } from "@/lib/auth";
import { AdminDashboardView } from "./_components/AdminDashboardView";

export const dynamic = "force-dynamic";

export default async function AdminDashboardPage() {
  const cookieStore = await cookies();
  const token = cookieStore.get("auth_token")?.value;
  const payload = token ? await verifyJWT(token) : null;
  if (!payload || payload.role !== "admin") {
    redirect("/portal/login?next=/admin/dashboard");
  }
  return <AdminDashboardView />;
}
