import RejectionMatrixClient from "../../components/rejection-matrix-client";
import { readDashboardState } from "../../lib/read-dashboard-state";


export const dynamic = "force-dynamic";


export default async function MatrixPage() {
  const initialData = await readDashboardState();
  return <RejectionMatrixClient initialData={initialData} />;
}