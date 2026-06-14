-- Migration 017: Pattern memory v2 — dimensiones correctas y bucketing grueso
-- 2026-06-14
--
-- Cambios vs v1 (datos v1 eran inútiles):
--   Bug 1: setup_type siempre era "smc_fvg" — reemplazado por fvg_tier real
--   Bug 2: atr_bucket siempre 0.0000 — eliminado del UNIQUE key (ruido en PRNG)
--   Bug 3: imminence_state faltaba — agregado leyendo imminence_state_at_entry
--   Bucketing grueso: score y hurst pasan a TEXT (3 categorías c/u)
--     → de ~4,800 buckets imposibles a ~1,296 alcanzables
--
-- Categorías v2:
--   fvg_tier      : mitigated | detected | none
--   hurst_bucket  : persistent | random | antipersistent  (H>0.55 / 0.45-0.55 / H<0.45)
--   imminence_state: ripe_or_overdue | building | fresh_or_dry
--   score_bucket  : medio (< 6.8) | alto (6.8-8.0) | excelente (≥ 8.0)
--
-- TRUNCATE: datos v1 tenían fvg_tier=NULL y atr_bucket=0, no recuperables.
-- Backup defensivo exportado a pattern_memory_backup_YYYYMMDD_HHMMSS.csv antes de aplicar.

-- 1. Truncar datos v1 corruptos
TRUNCATE TABLE ai_entry_pattern_memory RESTART IDENTITY;

-- 2. Eliminar UNIQUE constraint v1
ALTER TABLE ai_entry_pattern_memory
    DROP CONSTRAINT IF EXISTS ai_entry_pattern_memory_symbol_side_setup_type_regime_score_key;

-- 3. Agregar nuevas columnas dimensionales
ALTER TABLE ai_entry_pattern_memory
    ADD COLUMN IF NOT EXISTS fvg_tier      TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS imminence_state TEXT NOT NULL DEFAULT 'unknown';

-- 4. Cambiar score_bucket y hurst_bucket de NUMERIC a TEXT (categorías gruesas)
ALTER TABLE ai_entry_pattern_memory
    ALTER COLUMN score_bucket TYPE TEXT USING score_bucket::TEXT,
    ALTER COLUMN hurst_bucket TYPE TEXT USING hurst_bucket::TEXT;

-- 5. UNIQUE constraint v2: sin atr_bucket, con fvg_tier + imminence_state
ALTER TABLE ai_entry_pattern_memory
    ADD CONSTRAINT uq_pattern_memory_v2
    UNIQUE (symbol, side, fvg_tier, hurst_bucket, imminence_state, score_bucket);

-- 6. Metadatos de versión
COMMENT ON TABLE ai_entry_pattern_memory IS
'Pattern memory v2 (2026-06-14). UNIQUE key: symbol×side×fvg_tier×hurst_bucket×imminence_state×score_bucket. '
'fvg_tier: mitigated/detected/none. hurst_bucket: persistent/random/antipersistent. '
'imminence_state: ripe_or_overdue/building/fresh_or_dry. score_bucket: medio/alto/excelente. '
'atr_bucket y geo_bucket se conservan como columnas informativas (no forman parte de la clave).';
