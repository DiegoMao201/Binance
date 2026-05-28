-- ╔═══════════════════════════════════════════════════════════════════════════╗
-- ║  2026-05-28 — Simple Commission Model (capital_inicial + high watermark)  ║
-- ║                                                                           ║
-- ║  Anade columnas a la tabla `users` para soportar el modelo simplificado   ║
-- ║  de comision 20% sobre ganancia neta vs capital_inicial.                  ║
-- ║                                                                           ║
-- ║  IMPORTANTE:                                                              ║
-- ║   - NO renombra ni elimina columnas existentes.                           ║
-- ║   - NO toca columnas que lee el bot (balance_usdt, role, is_active,       ║
-- ║     performance_fee_pct).                                                 ║
-- ║   - Todas las columnas nuevas son nullable o tienen DEFAULT seguro.       ║
-- ║   - El bot Python sigue funcionando sin cambios (lee multi_accounts.json).║
-- ║                                                                           ║
-- ║  Mapeo de IDs (verificado contra DB prod 2026-05-28):                     ║
-- ║   - 66765271-50af-47ee-9096-2d0670de8cee  Diego (admin)                   ║
-- ║   - ded2ffb5-5307-4766-9fb8-a7b98fc2ddec  Diego Mauricio Garcia (principal)
-- ║   - 378397aa-0df4-4f8b-909c-232906da6e98  Angela Maria Contreras (esposa) ║
-- ╚═══════════════════════════════════════════════════════════════════════════╝

BEGIN;

-- ─── 1. Anade columnas nuevas ────────────────────────────────────────────────
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS capital_inicial         NUMERIC(28, 12),
    ADD COLUMN IF NOT EXISTS fecha_inicio            TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deriv_token             TEXT,
    ADD COLUMN IF NOT EXISTS deriv_account_id        VARCHAR(64),
    ADD COLUMN IF NOT EXISTS balance_actual_cache    NUMERIC(28, 12),
    ADD COLUMN IF NOT EXISTS balance_actual_at       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS comision_total_cobrada  NUMERIC(28, 12) NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_users_deriv_account_id
    ON users (deriv_account_id);
CREATE INDEX IF NOT EXISTS idx_users_fecha_inicio
    ON users (fecha_inicio);

-- ─── 2. Reset operativo: solo Angela activa como cliente ────────────────────
-- Admins quedan intactos. Diego-principal (cliente) queda inactivo en el
-- frontend; el bot sigue operando porque lee deriv_multi_accounts.json.
UPDATE users
   SET is_active = FALSE
 WHERE role IN ('client', 'investor')
   AND id <> '378397aa-0df4-4f8b-909c-232906da6e98'::uuid;

-- ─── 3. Configurar Angela con credenciales reales del JSON multi-accounts ────
UPDATE users
   SET display_name           = 'Angela Maria Contreras',
       capital_inicial        = 100.00,
       fecha_inicio           = COALESCE(fecha_inicio, NOW()),
       is_active              = TRUE,
       comision_total_cobrada = 0,
       deriv_token            = 'pat_a7a8a1e4022d033d0034d1072fd02d734f6826260328aee060dc5e90a7e6aa82',
       deriv_account_id       = 'DOT92477032'
 WHERE id = '378397aa-0df4-4f8b-909c-232906da6e98'::uuid;

COMMIT;
