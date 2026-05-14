import { PrismaClient } from "@prisma/client";

// Singleton pattern required for Next.js dev server (hot-reload creates new
// module instances, which would exhaust the PostgreSQL connection pool without
// this guard).
const globalForPrisma = globalThis as unknown as { prisma?: PrismaClient };

export const prisma: PrismaClient =
  globalForPrisma.prisma ??
  new PrismaClient({
    log:
      process.env.NODE_ENV === "development"
        ? ["query", "warn", "error"]
        : ["error"],
  });

if (process.env.NODE_ENV !== "production") {
  globalForPrisma.prisma = prisma;
}
