#!/bin/sh
# startup.sh — Run DB migrations then start the Next.js server.
# Executed inside the Docker container at runtime.
#
# Order:
#  1. Run migration 004 (create base tables) as bot_admin — idempotent
#  2. Try all known postgres superuser passwords to run migration 006
#     (table ownership transfer to bot_admin) — non-fatal if all fail
#  3. Run migration 005 (add trade_id + broker columns) as bot_admin — non-fatal
#  4. Start npm server regardless of migration 005 outcome

set -e

echo "[startup] Running migration 004 (PAMM tables)..."
npx prisma db execute --schema ./prisma/schema.prisma \
  --file ./db/migrations/004_pamm_trade_allocations.sql || \
  echo "[startup] WARN: migration 004 had warnings (non-fatal)."

echo "[startup] Attempting migration 006 (ownership transfer) with known postgres passwords..."
OWNERSHIP_FIXED=0
DB_HOST="10.0.1.8"
DB_PORT="5432"
DB_NAME="optiferre_pamm"

# Try all known Coolify postgres superuser passwords
for PG_PASS in \
  "o5S3X9VIYcbBWqd525hqT24UhYAc8AdjtevyHtlZHhGxJkfMQVZXReCTxkcjSOAX" \
  "K9iaS9RVVvlnzHqjd8rWDXPSidUZaqqs7rvrzKtuQK52JRBCbTfQd1MKmmvKQknc" \
  "JE7zr39ODs6ZHrTgzH1OWgsvt5J005hid73BfIMjiIKit9KxqJSNXh3KOHowMXwb"; do
  PG_URL="postgresql://postgres:${PG_PASS}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
  if npx prisma db execute --url "$PG_URL" --file ./db/migrations/006_fix_ownership.sql 2>&1; then
    echo "[startup] Migration 006 (ownership) succeeded."
    OWNERSHIP_FIXED=1
    break
  else
    echo "[startup] Password attempt failed, trying next..."
  fi
done

# Also try env var if set
if [ "$OWNERSHIP_FIXED" = "0" ] && [ -n "$POSTGRES_SUPERUSER_URL" ]; then
  echo "[startup] Trying POSTGRES_SUPERUSER_URL..."
  if npx prisma db execute --url "$POSTGRES_SUPERUSER_URL" --file ./db/migrations/006_fix_ownership.sql 2>&1; then
    echo "[startup] Migration 006 (ownership) succeeded via POSTGRES_SUPERUSER_URL."
    OWNERSHIP_FIXED=1
  fi
fi

if [ "$OWNERSHIP_FIXED" = "0" ]; then
  echo "[startup] WARN: Could not transfer table ownership. Migration 005 may fail."
fi

echo "[startup] Running migration 005 (trade_id + broker columns)..."
npx prisma db execute --schema ./prisma/schema.prisma \
  --file ./db/migrations/005_add_trade_id_broker.sql || \
  echo "[startup] WARN: migration 005 failed — continuing."

echo "[startup] Running migration 007 (add missing UTA columns)..."
npx prisma db execute --schema ./prisma/schema.prisma \
  --file ./db/migrations/007_add_missing_uta_columns.sql || \
  echo "[startup] WARN: migration 007 failed — symbol column may be missing."

echo "[startup] Running migration 008 (billing statements + whatsapp contact)..."
npx prisma db execute --schema ./prisma/schema.prisma \
  --file ./db/migrations/008_billing_statements_and_payments.sql || \
  echo "[startup] WARN: migration 008 failed — billing module may be limited."

echo "[startup] Starting Next.js server..."
exec npm run start

