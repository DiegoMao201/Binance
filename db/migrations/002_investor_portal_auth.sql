-- ╔═══════════════════════════════════════════════════════════════════════════╗
-- ║  Migration 002 — Investor Portal: Passwordless Auth                       ║
-- ║                                                                           ║
-- ║  Scope:                                                                   ║
-- ║    1. Alter `users` to support OTP-based login (drop NOT NULL on          ║
-- ║       password_hash, add entry_fee_pct, expand role values, fix           ║
-- ║       performance_fee_pct default to 5 %).                                ║
-- ║    2. Create `otp_codes` — time-bound, attempt-limited OTP store.         ║
-- ║                                                                           ║
-- ║  Safety:                                                                  ║
-- ║    • All ALTER TABLE operations are backward-compatible.                  ║
-- ║    • Bot's Python allocator uses users.role for display only, not         ║
-- ║      as a filter gate → adding 'client' value is safe.                   ║
-- ║    • Existing rows keep their current password_hash value unchanged.      ║
-- ╚═══════════════════════════════════════════════════════════════════════════╝

BEGIN;

-- ─── 1. password_hash → nullable ─────────────────────────────────────────────
-- The portal uses passwordless OTP auth. No password is ever stored.
-- Existing rows (if any) keep their hash; new rows will have NULL.
ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;


-- ─── 2. entry_fee_pct ────────────────────────────────────────────────────────
-- One-time onboarding fee charged as a % of each deposit.
-- Stored as a 0–1 ratio (0.02 = 2 %) matching the existing precision convention.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS entry_fee_pct NUMERIC(12, 8) NOT NULL DEFAULT 0.02
    CONSTRAINT users_entry_fee_pct_check CHECK (entry_fee_pct >= 0 AND entry_fee_pct <= 1);


-- ─── 3. Expand role CHECK to include 'client' ────────────────────────────────
-- 'investor' kept for backward compat with any existing rows.
-- New portal users will have role = 'client' or role = 'admin'.
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users
    ADD CONSTRAINT users_role_check CHECK (role IN ('investor', 'admin', 'client'));


-- ─── 4. Update performance_fee_pct default to 0.05 (5 %) ────────────────────
-- Existing rows keep their current value. Only new rows get the new default.
ALTER TABLE users ALTER COLUMN performance_fee_pct SET DEFAULT 0.05;


-- ─── 5. otp_codes ────────────────────────────────────────────────────────────
-- One active OTP per user at a time (enforced at the application layer by
-- expiring previous codes before inserting a new one).
--
-- Security properties:
--   • code_hash stores SHA-256(otp_plaintext) — plaintext never persisted.
--   • expires_at: 10-minute TTL enforced by the application AND index filter.
--   • attempts: invalidated after 3 failed verifications (app-layer check).
CREATE TABLE IF NOT EXISTS otp_codes (
    id          BIGSERIAL    PRIMARY KEY,
    user_id     UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash   TEXT         NOT NULL,
    expires_at  TIMESTAMPTZ  NOT NULL,
    attempts    INT          NOT NULL DEFAULT 0
                             CONSTRAINT otp_attempts_check CHECK (attempts >= 0),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Partial index: fast lookup of "live" OTPs without scanning expired rows.
CREATE INDEX IF NOT EXISTS idx_otp_active
    ON otp_codes (user_id, expires_at)
    WHERE attempts < 3;

COMMIT;
