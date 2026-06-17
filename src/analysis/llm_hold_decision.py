"""
LLM Hold Decision — Filosofía A (2026-06-14)

Opinión contextual del LLM durante el hold de un contrato abierto.
NO reemplaza la lógica estructural — la complementa cuando hay ambigüedad.

Principios:
- Solo se invoca cuando hay un trigger concreto (no en cada tick)
- Fallback siempre es HOLD (no cerrar por error del LLM)
- Alta barra para CLOSE: confidence >= 0.80
- Rate limit: 1 llamada por contrato cada 60s mínimo
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

_OPENROUTER_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
_OPENROUTER_URL: str = "https://openrouter.ai/api/v1/chat/completions"
_HOLD_LLM_MODELS: list[str] = [
    m.strip()
    for m in os.getenv(
        "DERIV_HOLD_LLM_MODELS",
        "google/gemini-2.5-flash,openai/gpt-4o-mini",
    ).split(",")
    if m.strip()
]

_HOLD_LLM_ENABLED: bool = (
    os.getenv("DERIV_HOLD_LLM_ENABLED", "false").lower().strip() in ("1", "true", "yes", "on")
)
_HOLD_LLM_MIN_INTERVAL_SEC: float = float(
    os.getenv("DERIV_HOLD_LLM_MIN_INTERVAL_SEC", "60") or 60
)
_HOLD_LLM_MIN_CLOSE_CONFIDENCE: float = float(
    os.getenv("DERIV_HOLD_LLM_MIN_CLOSE_CONFIDENCE", "0.80") or 0.80
)

# Paso 7 — MAX_HOLD respiro inteligente
_RESPITE_ENABLED: bool = (
    os.getenv("DERIV_MAX_HOLD_RESPITE_ENABLED", "true").lower().strip() in ("1", "true", "yes", "on")
)
_RESPITE_MAX_EXTENSION_SEC: float = float(
    os.getenv("DERIV_MAX_HOLD_RESPITE_MAX_SEC", "120") or 120
)
_RESPITE_MIN_CONFIDENCE: float = float(
    os.getenv("DERIV_MAX_HOLD_RESPITE_MIN_CONFIDENCE", "0.75") or 0.75
)

# Percentiles empíricos de ganadores por símbolo (data 2026-06-14)
# BOOM900/BOOM1000/CRASH1000: estimación inicial conservadora (PASO 2.3e-G 2026-06-16).
# Recalibrar con data real tras 7-14 días de observación por símbolo.
_WINNER_PERCENTILES: dict[str, dict[str, int]] = {
    "BOOM500":  {"p25": 81,  "p50": 381,  "p75": 610},
    "BOOM600":  {"p25": 217, "p50": 256,  "p75": 562},
    "CRASH500": {"p25": 264, "p50": 275,  "p75": 792},
    "CRASH600": {"p25": 300, "p50": 400,  "p75": 600},
    "CRASH900": {"p25": 675, "p50": 708,  "p75": 1310},
    "BOOM900":  {"p25": 200, "p50": 400,  "p75": 600},
    "BOOM1000": {"p25": 200, "p50": 400,  "p75": 700},
    "CRASH1000":{"p25": 200, "p50": 450,  "p75": 700},
}

# Razones estructurales válidas para CLOSE (anti-alucinación: cualquier otro código → HOLD)
_VALID_CLOSE_REASONS: frozenset[str] = frozenset({
    "SPIKE_OPUESTO",
    "LIQUIDEZ_MACRO_ADVERSA",
    "REGIMEN_MUERTO",
    "FVG_INVALIDADO",
})

# Rate limit: contract_id → last call ts
_last_call_ts: dict[str | int, float] = {}


@dataclass
class RespiteDecision:
    grant_respite: bool
    extension_sec: int
    confidence: float
    reason: str


@dataclass
class HoldDecision:
    action: str       # "HOLD" | "CLOSE"
    confidence: float
    reason: str
    invoked: bool = True  # False si no se invocó por rate limit u otro filtro


def should_invoke_hold_llm(
    contract: object,
    held_sec: float,
    current_state: dict,
) -> bool:
    """Decide si vale la pena invocar el LLM (costoso por llamada HTTP)."""
    if not _HOLD_LLM_ENABLED:
        return False

    cid = getattr(contract, "contract_id", None) or getattr(contract, "id", None)
    if cid is None:
        return False

    # Rate limit por contrato
    if time.time() - _last_call_ts.get(cid, 0.0) < _HOLD_LLM_MIN_INTERVAL_SEC:
        return False

    sym: str = getattr(contract, "symbol", "")
    pctls = _WINNER_PERCENTILES.get(sym, {"p75": 600})
    p75 = pctls.get("p75", 600)
    peak = float(getattr(contract, "peak_profit", 0) or 0)

    # Trigger 1: llevamos > 65% del max_hold del símbolo sin spike.
    # El umbral usa min(p75, sym_max_hold)*0.65 para que el LLM siempre
    # dispare ANTES del cierre por max_hold (el p75*0.80 anterior superaba
    # el max_hold en BOOM500/CRASH500/CRASH900, dejando el LLM sin voz).
    _sym_max_hold = float(current_state.get("sym_max_hold", p75) or p75)
    _llm_trigger_threshold = min(p75, _sym_max_hold) * 0.65
    trigger_long_hold_no_peak = (held_sec > _llm_trigger_threshold and peak == 0.0)

    # Trigger 2: market_phase degrada a CAUTION
    trigger_phase_caution = current_state.get("market_phase") == "CAUTION"

    # Trigger 3: estructura ambigua detectada por el caller
    trigger_structural_ambiguous = bool(current_state.get("structural_ambiguous", False))

    return any([trigger_long_hold_no_peak, trigger_phase_caution, trigger_structural_ambiguous])


async def evaluate_hold_with_llm(
    contract: object,
    current_state: dict,
) -> HoldDecision:
    """
    Invoca el LLM con contexto estructural real del trade.
    Fallback seguro: HOLD si el LLM falla o no hay clave.

    current_state debe contener (inyectado en el call site):
        sym_max_hold, market_phase, fvg_still_valid,
        opposite_spike_recent, liquidez_adversa, imminence_state_current
    """
    if not _OPENROUTER_KEY:
        return HoldDecision("HOLD", 0.0, "no_api_key", invoked=False)

    sym: str = getattr(contract, "symbol", "")
    side: str = getattr(contract, "side", "")
    peak: float = float(getattr(contract, "peak_profit", 0) or 0)
    floating_pnl: float = float(getattr(contract, "floating_pnl", 0) or 0)
    opened_at_ts: float = float(getattr(contract, "opened_at_ts", 0) or 0)
    held_sec: float = time.time() - opened_at_ts

    score_bd: dict = getattr(contract, "score_breakdown", None) or {}
    entry_score = score_bd.get("score_at_entry") or score_bd.get("score") or "N/A"
    setup_type = score_bd.get("setup_type", "unknown") or "unknown"
    entry_imminence = score_bd.get("spike_imminence_state") or "unknown"

    sym_max_hold = float(current_state.get("sym_max_hold", 750) or 750)
    pctls = _WINNER_PERCENTILES.get(sym, {"p25": 0, "p50": 0, "p75": 600})
    p75 = pctls.get("p75", 600)
    effective_deadline = int(min(p75, sym_max_hold * 0.85))
    time_to_max_hold = max(0, int(sym_max_hold - held_sec))

    opposite_spike = bool(current_state.get("opposite_spike_recent", False))
    liquidez_adversa = bool(current_state.get("liquidez_adversa", False))
    imm_current = current_state.get("imminence_state_current", "unknown") or "unknown"
    market_phase = current_state.get("market_phase", "unknown") or "unknown"
    fvg_valid = current_state.get("fvg_still_valid", "unknown") or "unknown"

    prompt = (
        f"Eres especialista en trading de índices sintéticos Deriv (PRNG Poisson).\n\n"
        f"CONTEXTO DEL TRADE ABIERTO:\n"
        f"- Símbolo: {sym}\n"
        f"- Dirección: {side}\n"
        f"- Held seconds: {held_sec:.0f}s de max {sym_max_hold:.0f}s\n"
        f"- PnL flotante actual: ${floating_pnl:+.2f}\n"
        f"- Peak profit alcanzado: ${peak:+.2f}\n"
        f"- Score de entrada: {entry_score}\n"
        f"- Setup type al entrar: {setup_type}\n"
        f"- Imminence state al entrar: {entry_imminence}\n\n"
        f"ESTADO ESTRUCTURAL ACTUAL:\n"
        f"- Régimen actual (market_phase): {market_phase}\n"
        f"- FVG todavía válido: {fvg_valid}\n"
        f"- Imminence state actual: {imm_current}\n"
        f"- Spike opuesto detectado últimos 60s: {opposite_spike}\n"
        f"- Zona liquidez macro adversa (Vision LLM): {liquidez_adversa}\n\n"
        f"REGLAS DE DECISIÓN (FILOSOFÍA PRNG NO NEGOCIABLE):\n\n"
        f"REGLA 1 — DEFAULT ES HOLD\n"
        f"A menos que veas EVIDENCIA ESTRUCTURAL ESPECÍFICA, responde HOLD.\n"
        f"La paciencia tiene valor en PRNG. El drift inter-spike es ruido sin valor predictivo.\n\n"
        f"REGLA 2 — RAZONES VÁLIDAS PARA CLOSE (deben ser estructurales):\n"
        f"  (A) SPIKE_OPUESTO: opposite_spike_recent=true — un spike contra nuestro side acaba de ocurrir.\n"
        f"  (B) LIQUIDEZ_MACRO_ADVERSA: liquidez_adversa=true — Vision LLM detectó macro contra nuestro side.\n"
        f"  (C) REGIMEN_MUERTO: market_phase=DEAD + imminence_state en DRY/OVERDUE — sequía estructural.\n"
        f"  (D) FVG_INVALIDADO: fvg_still_valid=no — la estructura que justificó la entrada ya no existe.\n\n"
        f"REGLA 3 — RAZONES NO VÁLIDAS (rechazar incluso si parecen tentadoras):\n"
        f"  NO: 'Lleva mucho tiempo' — tiempo solo NO es razón en PRNG\n"
        f"  NO: 'PnL negativo' — flotante negativo es normal pre-spike\n"
        f"  NO: 'Score bajó' — score post-entrada es ruido en PRNG\n"
        f"  NO: 'Hurst cambió' — Hurst es para entrada, no para hold\n\n"
        f"REGLA 4 — DEADLINE EFECTIVO DEL TRADE:\n"
        f"  Deadline efectivo = min(p75={p75}s, max_hold*0.85={sym_max_hold*0.85:.0f}s) = {effective_deadline}s.\n"
        f"  El bot cerrará en {time_to_max_hold}s de todos modos.\n"
        f"  ANTES del deadline ({held_sec:.0f}s < {effective_deadline}s): bias hacia HOLD.\n"
        f"  DESPUÉS del deadline: las razones (A)-(D) pesan más; evalúa con más rigor.\n\n"
        f"REGLA 5 — CONFIDENCE: mínimo 0.80 para CLOSE. Menos → responde HOLD.\n\n"
        f"RESPONDE JSON ESTRICTO SIN MARKDOWN:\n"
        f'{{"action": "HOLD" or "CLOSE", "confidence": 0.0-1.0, '
        f'"reason_code": "SPIKE_OPUESTO" | "LIQUIDEZ_MACRO_ADVERSA" | "REGIMEN_MUERTO" | "FVG_INVALIDADO" | "NO_STRUCTURAL_EVIDENCE", '
        f'"reason": "frase corta basada en señales observables"}}'
    )

    import aiohttp

    cid = getattr(contract, "contract_id", None) or getattr(contract, "id", None)
    _last_call_ts[cid] = time.time()

    for model in _HOLD_LLM_MODELS:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 200,
        }
        headers = {
            "Authorization": f"Bearer {_OPENROUTER_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://optiferre.app",
            "X-Title": "OptiFerre-Deriv-Hold",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _OPENROUTER_URL,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10.0),
                ) as resp:
                    if resp.status not in (200,):
                        _LOGGER.warning("[HOLD_LLM] %s HTTP %s on %s", sym, resp.status, model)
                        continue
                    data = await resp.json()
                    raw = data["choices"][0]["message"]["content"].strip()
                    if raw.startswith("```"):
                        raw = raw.split("```")[1]
                        if raw.startswith("json"):
                            raw = raw[4:]
                        raw = raw.strip()
                    parsed = json.loads(raw)
        except asyncio.TimeoutError:
            _LOGGER.warning("[HOLD_LLM] %s timeout on %s", sym, model)
            continue
        except Exception as exc:
            _LOGGER.warning("[HOLD_LLM] %s error on %s: %s", sym, model, exc)
            continue

        action = str(parsed.get("action", "HOLD")).upper()
        confidence = float(parsed.get("confidence", 0.5))
        reason_code = str(parsed.get("reason_code", "NO_STRUCTURAL_EVIDENCE")).upper()
        reason = str(parsed.get("reason", ""))

        # Guard anti-alucinación: CLOSE solo si reason_code es uno de los 4 válidos
        if action == "CLOSE" and reason_code not in _VALID_CLOSE_REASONS:
            _LOGGER.warning(
                "[HOLD_LLM] %s dijo CLOSE con reason_code='%s' no válido → HOLD (anti-alucinación)",
                sym, reason_code,
            )
            action = "HOLD"
            confidence = 0.5
            reason = f"invalid_reason_code ({reason_code}): {reason}"

        # Alta barra para cerrar: requiere confidence >= threshold
        if action == "CLOSE" and confidence < _HOLD_LLM_MIN_CLOSE_CONFIDENCE:
            _LOGGER.info(
                "[HOLD_LLM] %s dijo CLOSE conf=%.2f < %.2f → HOLD por baja confianza",
                sym, confidence, _HOLD_LLM_MIN_CLOSE_CONFIDENCE,
            )
            action = "HOLD"
            reason = f"low_confidence_close ({reason})"

        _LOGGER.info(
            "[HOLD_LLM] %s held=%.0fs action=%s conf=%.2f rc=%s reason='%s' "
            "opp_spike=%s liq_adversa=%s imm=%s phase=%s model=%s",
            sym, held_sec, action, confidence, reason_code, reason,
            opposite_spike, liquidez_adversa, imm_current, market_phase, model,
        )
        return HoldDecision(action=action, confidence=confidence, reason=f"{reason_code}: {reason}", invoked=True)

    # Todos los modelos fallaron → HOLD seguro
    _LOGGER.warning("[HOLD_LLM] %s all models failed → default HOLD", sym)
    return HoldDecision("HOLD", 0.0, "all_models_failed", invoked=True)


async def _call_llm_with_fallback(prompt: str, temperature: float = 0.1) -> str:
    """Call LLM with fallback models. Returns content text or raises RuntimeError."""
    import aiohttp
    for model in _HOLD_LLM_MODELS:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": 120,
        }
        headers = {
            "Authorization": f"Bearer {_OPENROUTER_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://optiferre.app",
            "X-Title": "OptiFerre-Deriv-Hold",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _OPENROUTER_URL,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10.0),
                ) as resp:
                    if resp.status != 200:
                        _LOGGER.warning("[LLM_CALL] HTTP %s on %s", resp.status, model)
                        continue
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"].strip()
        except asyncio.TimeoutError:
            _LOGGER.warning("[LLM_CALL] timeout on %s", model)
            continue
        except Exception as exc:
            _LOGGER.warning("[LLM_CALL] error on %s: %s", model, exc)
            continue
    raise RuntimeError("all_models_failed")


async def evaluate_max_hold_respite(contract: object, current_state: dict) -> RespiteDecision:
    """
    Pregunta al LLM si vale la pena dar respiro al max_hold.
    Solo se llama UNA vez por contrato (justo al llegar al max_hold base).
    Si concede, extiende hasta DERIV_MAX_HOLD_RESPITE_MAX_SEC (default 120s).
    Fallback seguro: DENY si el LLM falla o no hay clave.
    """
    if not _RESPITE_ENABLED:
        return RespiteDecision(grant_respite=False, extension_sec=0, confidence=0.0, reason="feature_disabled")

    if not _OPENROUTER_KEY:
        return RespiteDecision(grant_respite=False, extension_sec=0, confidence=0.0, reason="no_api_key")

    sym: str = getattr(contract, "symbol", "")
    pctls = _WINNER_PERCENTILES.get(sym, {"p25": 0, "p50": 0, "p75": 0})
    held_sec = time.time() - float(getattr(contract, "opened_at_ts", 0) or 0)

    prompt = f"""Eres especialista en índices sintéticos Deriv (PRNG Poisson).

CONTEXTO: el contrato {sym} ({getattr(contract, 'side', '')}) llegó a su max_hold de {held_sec:.0f}s \
sin capturar spike (peak_profit = $0.00).

DECISIÓN A TOMAR: ¿le doy un RESPIRO corto (máximo 120s extra) o lo cierro ya?

DATOS PARA TU DECISIÓN:
- Imminence state actual: {current_state.get('imminence_state', 'unknown')}
- Imminence score: {current_state.get('imminence_score', 'unknown')}
- Scarcity state: {current_state.get('scarcity_state', 'unknown')}
- Scarcity ratio: {current_state.get('scarcity_ratio', 'unknown')}
- Spikes recientes en símbolos hermanos (mismo lado): {current_state.get('sibling_spikes_recent', 0)} en últimos 60s
- market_phase: {current_state.get('market_phase', 'unknown')}
- FVG estructural sigue válido: {current_state.get('fvg_still_valid', 'unknown')}

PERCENTILES DE GANADORES {sym}:
- p50: {pctls['p50']}s
- p75: {pctls['p75']}s

REGLAS:
1. SOLO concede respiro si hay señales empíricas REALES de inminencia:
   - imminence_state == "RIPE" o "OVERDUE" (pico modal de spikes)
   - scarcity_state == "SECO" o "VENCIDO" (presión acumulada extrema)
   - spike reciente en símbolo hermano del mismo lado
2. NO concedas respiro por: score alto, RNG alto, intuición, "se siente cerca"
3. Si NINGUNA de las señales empíricas dice inminencia → CIERRA (deny respite)
4. Si hold_sec ya supera el p75 ({pctls['p75']}s) Y no hay señales fuertes → CIERRA
5. Confianza alta (>= 0.75) requerida para conceder

Responde JSON estricto sin markdown:
{{
  "grant_respite": true | false,
  "extension_sec": 0-120,
  "confidence": 0.0-1.0,
  "reason": "frase corta basada en señales empíricas concretas"
}}
"""

    try:
        response_text = await _call_llm_with_fallback(prompt, temperature=0.1)

        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()

        data = json.loads(cleaned)

        grant = bool(data.get("grant_respite", False))
        extension = int(data.get("extension_sec", 0))
        confidence = float(data.get("confidence", 0.0))
        reason = str(data.get("reason", "no reason"))

        extension = min(extension, int(_RESPITE_MAX_EXTENSION_SEC))

        if grant and confidence < _RESPITE_MIN_CONFIDENCE:
            _LOGGER.info(
                "[MAX_HOLD_RESPITE] %s LLM concedió respiro pero conf=%.2f < %.2f → DENY",
                sym, confidence, _RESPITE_MIN_CONFIDENCE,
            )
            grant = False
            reason = f"low_confidence ({reason})"

        if not grant:
            extension = 0

        return RespiteDecision(
            grant_respite=grant,
            extension_sec=extension,
            confidence=confidence,
            reason=reason,
        )

    except Exception as e:
        _LOGGER.warning("[MAX_HOLD_RESPITE] %s error: %s → DENY por defecto", sym, e)
        return RespiteDecision(
            grant_respite=False,
            extension_sec=0,
            confidence=0.0,
            reason=f"llm_error: {e}",
        )
