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

    # Trigger 1: llevamos > 80% del p75 sin haber visto ningún movimiento
    trigger_long_hold_no_peak = (held_sec > p75 * 0.80 and peak == 0.0)

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
