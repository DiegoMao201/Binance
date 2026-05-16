export const dynamic = "force-dynamic";

import DerivAnalyticsClient from "../../components/deriv-analytics-client";

export default async function DerivPage() {
  // Initial server-side fetch
  let initialData = {};
  try {
    const { readDashboardState } = await import("../../lib/read-dashboard-state");
    const state = await readDashboardState();
    initialData = {
      derivStatus: state.derivStatus || {},
      derivOpenContracts: state.derivOpenContracts || [],
      derivClosedContracts: state.derivClosedContracts || [],
    };
  } catch (e) {
    console.error("[deriv-page] initial fetch failed:", e?.message);
  }
  return <DerivAnalyticsClient {...initialData} />;
}
