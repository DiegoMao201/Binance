import { NextResponse } from "next/server";

import { readDashboardState } from "../../../lib/read-dashboard-state";


export const dynamic = "force-dynamic";


export async function GET() {
  const payload = await readDashboardState();
  return NextResponse.json(payload, {
    headers: {
      "Cache-Control": "no-store, max-age=0",
    },
  });
}