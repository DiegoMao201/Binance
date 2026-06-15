"""
LLM Hold Decision — Filosofía A (2026-06-14)

Opinión contextual del LLM durante el hold de un contrato abierto.
NO reemplaza la lógica estructural — la complementa cuando hay ambigüedad.

Principios:
- Solo se invoca cuando hay un trigger concreto (no en cada tick)
- Fallback siempre es HOLD (no cerrar por error del LLM)
- Alta barra para CLOSE: confidence >= 0.70
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
    os.getenv("DERIV_HOLD_LLM_ENABLED", "true").lower().strip() in ("1", "true", "yes", "on")
)
_HOLD_LLM_MIN_INTERVAL_SEC: float = float(
    os.getenv("DERIV_HOLD_LLM_MIN_INTERVAL_SEC", "60") or 60
)
_HOLD_LLM_MIN_CLOSE_CONFIDENCE: float = float(
    os.getenv("DERIV_HOLD_LLM_MIN_CLOSE_CONFIDENCE", "0.70") or 0.70
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
_WINNER_PERCENTILES: dict[str, dict[str, int]] = {
    "BOOM500":  {"p25": 81,  "p50": 381,  "p75": 610},
    "BOOM600":  {"p25": 217, "p50": 256,  "p75": 562},
    "CRASH500": {"p25": 264, "p50": 275,  "p75": 792},
    "CRASH600": {"p25": 300, "p50": 400,  "p75": 600},
    "CRASH900": {"p25": 675, "p50": 708,  "p75": 1310},
}

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
    Invoca el LLM con contexto completo del trade.
    Fallback seguro: HOLD si el LLM falla o no hay clave.
    """
    if not _OPENROUTER_KEY:
        return HoldDecision("HOLD", 0.0, "no_api_key", invoked=False)

    sym: str = getattr(contract, "symbol", "")
    side: str = getattr(contract, "side", "")
    peak: float = float(getattr(contract, "peak_profit", 0) or 0)
    floating_pnl: float = float(getattr(contract, "floating_pnl", 0) or 0)
    opened_at_ts: float = float(getattr(contract, "opened_at_ts", 0) or 0)
    held_sec: float = time.time() - opened_at_ts
    max_hold: float = float(getattr(contract, "max_hold_seconds", 0) or 0)

    score_bd: dict = getattr(contract, "score_breakdown", None) or {}
    entry_score = score_bd.get("score_at_entry") or score_bd.get("score") or "N/A"
    entry_fvg_tier = score_bd.get("fvg_tier") or current_state.get("entry_fvg_tier", "N/A")
    entry_imminence = score_bd.get("spike_imminence_state") or current_state.get("entry_imminence", "N/A")
    entry_scarcity = score_bd.get("scarcity_state") or current_state.get("entry_scarcity", "N/A")

    pctls = _WINNER_PERCENTILES.get(sym, {"p25": 0, "p50": 0, "p75": 600})

    prompt = (
        f"Eres un especialista en trading de índices sintéticos Deriv (PRNG Poisson).\n\n"
        f"CONTRATO ACTIVO:\n"
        f"- Símbolo: {sym}\n"
        f"- Lado: {side}\n"
        f"- Entró hace: {held_sec:.0f}s\n"
        f"- max_hold: {max_hold:.0f}s\n"
        f"- peak_profit actual: ${peak:.2f}\n"
        f"- PnL flotante: ${floating_pnl:.2f}\n\n"
        f"CONDICIONES AL ENTRAR:\n"
        f"- score: {entry_score}\n"
        f"- FVG tier: {entry_fvg_tier}\n"
        f"- imminence: {entry_imminence}\n"
        f"- scarcity: {entry_scarcity}\n\n"
        f"CONDICIONES AHORA (las únicas que importan):\n"
        f"- FVG estructural sigue válido: {current_state.get('fvg_still_valid', 'unknown')}\n"
        f"- BOS opuesto detectado: {current_state.get('bos_opposite', False)}\n"
        f"- Spike opuesto últimos 60s: {current_state.get('opposite_spike_recent', False)}\n"
        f"- market_phase: {current_state.get('market_phase', 'unknown')}\n\n"
        f"DATA HISTÓRICA GANADORES {sym}:\n"
        f"- p25: {pctls['p25']}s  p50: {pctls['p50']}s  p75: {pctls['p75']}s\n\n"
        f"REGLAS NO NEGOCIABLES:\n"
        f"1. NO cierres por score/RNG bajos — drift normal del PRNG\n"
        f"2. NO cierres por tiempo — solo por evento estructural\n"
        f"3. SÍ cierra si: BOS opuesto confirmado, spike opuesto, market_phase=DEAD\n"
        f"4. Si held_sec < p75 ({pctls['p75']}s), dar más tiempo salvo evento estructural claro\n"
        f"5. Si held_sec > p75 y peak=0 y estructura degradada, considera CLOSE\n\n"
        f"Responde JSON estricto sin markdown:\n"
        f'{{\"action\": \"HOLD\" or \"CLOSE\", \"confidence\": 0.0-1.0, \"reason\": \"frase corta\"}}'
    )

    import aiohttp

    cid = getattr(contract, "contract_id", None) or getattr(contract, "id", None)
    _last_call_ts[cid] = time.time()

    for model in _HOLD_LLM_MODELS:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
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
        reason = str(parsed.get("reason", ""))

        # Alta barra para cerrar: requiere confidence >= threshold
        if action == "CLOSE" and confidence < _HOLD_LLM_MIN_CLOSE_CONFIDENCE:
            _LOGGER.info(
                "[HOLD_LLM] %s dijo CLOSE conf=%.2f < %.2f → HOLD por baja confianza",
                sym, confidence, _HOLD_LLM_MIN_CLOSE_CONFIDENCE,
            )
            action = "HOLD"
            reason = f"low_confidence_close ({reason})"

        _LOGGER.info(
            "[HOLD_LLM] %s held=%.0fs action=%s conf=%.2f reason='%s' model=%s",
            sym, held_sec, action, confidence, reason, model,
        )
        return HoldDecision(action=action, confidence=confidence, reason=reason, invoked=True)

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
