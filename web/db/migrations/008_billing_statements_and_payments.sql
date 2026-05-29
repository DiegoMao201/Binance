-- 2026-05-29
-- Billing control: statements, payment tracking, and WhatsApp contact

BEGIN;

ALTER TABLE users
  ADD COLUMN IF NOT EXISTS billing_whatsapp VARCHAR(32);

CREATE INDEX IF NOT EXISTS idx_users_billing_whatsapp
  ON users (billing_whatsapp)
  WHERE billing_whatsapp IS NOT NULL;

CREATE TABLE IF NOT EXISTS client_billing_statements (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  period_start DATE NOT NULL,
  period_end DATE NOT NULL,
  mode VARCHAR(24) NOT NULL DEFAULT 'since_last_payment',

  trades_count INTEGER NOT NULL DEFAULT 0,
  pnl_usdt NUMERIC(28,12) NOT NULL DEFAULT 0,
  service_due_usdt NUMERIC(28,12) NOT NULL DEFAULT 0,
  client_net_usdt NUMERIC(28,12) NOT NULL DEFAULT 0,
  capital_start_usdt NUMERIC(28,12) NOT NULL DEFAULT 0,
  capital_end_usdt NUMERIC(28,12) NOT NULL DEFAULT 0,

  status VARCHAR(16) NOT NULL DEFAULT 'pending',
  paid_amount_usdt NUMERIC(28,12) NOT NULL DEFAULT 0,
  paid_at TIMESTAMPTZ,
  payment_channel VARCHAR(32),
  payment_reference VARCHAR(160),
  notes TEXT,
  email_sent_at TIMESTAMPTZ,

  statement_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  CONSTRAINT chk_client_billing_status
    CHECK (status IN ('pending','paid','waived')),
  CONSTRAINT chk_client_billing_period
    CHECK (period_end >= period_start),
  CONSTRAINT uq_client_billing_period
    UNIQUE (user_id, period_start, period_end)
);

CREATE INDEX IF NOT EXISTS idx_client_billing_user_status
  ON client_billing_statements(user_id, status);

CREATE INDEX IF NOT EXISTS idx_client_billing_period_end
  ON client_billing_statements(period_end DESC);

CREATE INDEX IF NOT EXISTS idx_client_billing_status
  ON client_billing_statements(status);

COMMIT;
