import DashboardClient from "../components/operator-terminal";
import { readDashboardState } from "../lib/read-dashboard-state";


export const dynamic = "force-dynamic";


export default async function Page() {
  const initialData = await readDashboardState();
  return <DashboardClient initialData={initialData} />;
}