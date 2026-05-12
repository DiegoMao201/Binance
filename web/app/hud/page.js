import HudClient from "../../components/hud/hud-client";
import { readDashboardState } from "../../lib/read-dashboard-state";
import "./hud.css";

export const dynamic = "force-dynamic";

export const metadata = { title: "OptiFerre HUD · Real-Time Terminal" };

export default async function HudPage() {
  const initialData = await readDashboardState();
  return <HudClient initialData={initialData} />;
}
