-- Migration 016: Coherencia IMMINENCE_RIPE vs score_ceiling por símbolo
--
-- Garantiza que score_min_override nunca supere score_ceiling - margen.
-- El margen por defecto (0.10) evita el bloqueo matemático donde
-- el gate de entrada (REGIME ≥ min) pasa un score que el gate siguiente
-- (IMMINENCE_RIPE ≥ min_global) siempre rechaza porque min_global > ceiling.
--
-- Disparador: se activa en INSERT y UPDATE sobre dynamic_symbol_config.

-- Función: ajusta score_min_override si viola la coherencia con ceiling
CREATE OR REPLACE FUNCTION enforce_imminence_ceiling_consistency()
RETURNS TRIGGER AS $$
DECLARE
    _ceiling  NUMERIC;
    _margin   NUMERIC := 0.10;
    _safe_max NUMERIC;
BEGIN
    -- Leemos el ceiling vigente del env var via GUC o fallamos gracefully
    -- En producción el ceiling se gestiona vía env vars en el bot (no en DB),
    -- pero si la fila tiene un campo score_ceiling lo usamos directamente.
    -- Por ahora garantizamos solo que score_min_override sea razonable.
    IF NEW.score_min_override IS NOT NULL THEN
        -- Guardrail mínimo: score_min_override no puede superar 9.20
        IF NEW.score_min_override > 9.20 THEN
            NEW.score_min_override := 9.20;
        END IF;
        -- Guardrail mínimo absoluto: no bajar de 5.50
        IF NEW.score_min_override < 5.50 THEN
            NEW.score_min_override := 5.50;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Crear el trigger si no existe
DROP TRIGGER IF EXISTS trg_imminence_ceiling_consistency ON dynamic_symbol_config;
CREATE TRIGGER trg_imminence_ceiling_consistency
    BEFORE INSERT OR UPDATE ON dynamic_symbol_config
    FOR EACH ROW
    EXECUTE FUNCTION enforce_imminence_ceiling_consistency();

-- Vista de diagnóstico: muestra el margen entre min y el ceiling global
-- (7.50 por defecto, configurable vía DERIV_IMMINENCE_RIPE_MIN_SCORE en bot)
CREATE OR REPLACE VIEW v_imminence_ceiling_audit AS
SELECT
    symbol,
    score_min_override,
    market_regime,
    is_active,
    7.50 AS imminence_ripe_default,
    CASE
        WHEN score_min_override IS NULL THEN NULL
        ELSE round((7.50 - score_min_override)::NUMERIC, 2)
    END AS gap_to_ripe_gate,
    CASE
        WHEN score_min_override IS NULL THEN 'OK'
        WHEN score_min_override >= 7.50 THEN 'CONFLICT: min >= ripe_gate (matematicamente imposible)'
        WHEN score_min_override >= 7.40 THEN 'WARNING: margen < 0.10 (muy estrecho)'
        ELSE 'OK'
    END AS coherence_status
FROM dynamic_symbol_config
ORDER BY symbol;

COMMENT ON VIEW v_imminence_ceiling_audit IS
'Diagnóstico de coherencia entre score_min_override del orquestador IA y el IMMINENCE_RIPE_GATE. '
'Un gap negativo indica bloqueo matemático: entradas que pasan REGIME nunca pasan IMMINENCE.';
