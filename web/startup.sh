#!/bin/sh
# startup.sh — Run DB migrations then start the Next.js server.
# Executed inside the Docker container at runtime.
#
# Order:
#  1. Run migration 004 (create base tables) as bot_admin — idempotent
#  2. If POSTGRES_SUPERUSER_URL is set, run migration 006 as postgres
#     to transfer table ownership to bot_admin (one-time fix)
#  3. Run migration 005 (add trade_id + broker columns) as bot_admin
#  4. Start npm server

set -e

echo "[startup] Running migration 004 (PAMM tables)..."
npx prisma db execute --schema ./prisma/schema.prisma \
  --file ./db/migrations/004_pamm_trade_allocations.sql

if [ -n "$POSTGRES_SUPERUSER_URL" ]; then
  echo "[startup] Running migration 006 (ownership transfer to bot_admin) as postgres superuser..."
  npx prisma db execute --url "$POSTGRES_SUPERUSER_URL" \
    --file ./db/migrations/006_fix_ownership.sql || \
    echo "[startup] WARN: migration 006 ownership transfer failed (may already be done) — continuing"
else
  echo "[startup] POSTGRES_SUPERUSER_URL not set — skipping ownership transfer (migration 006)"
fi

echo "[startup] Running migration 005 (trade_id + broker columns)..."
npx prisma db execute --schema ./prisma/schema.prisma \
  --file ./db/migrations/005_add_trade_id_broker.sql

echo "[startup] Starting Next.js server..."
exec npm run start
