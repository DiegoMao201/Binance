"""
Structural Exit Decision — Filosofía A (2026-06-14)

Solo las 4 condiciones estructurales validan un cierre mid-trade:
1. SPIKE_OPPOSITE_DETECTED — el PRNG disparó contra la posición
2. FVG_BOS_OPPOSITE — Break of Structure en dirección contraria
3. SYMBOL_DEACTIVATED_BY_ORCHESTRATOR — orquestador marcó is_active=False
4. MARKET_PHASE_DEAD — el PRNG está efectivamente pausado

Todo lo demás (score bajo, RNG bajo, tiempo transcurrido) es drift normal
del PRNG y NO es razón estructural para cerrar.
"""
from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

_STRUCTURAL_EXIT_ENABLED: bool = (
    os.getenv("DERIV_STRUCTURAL_EXIT_ENABLED", "true").lower().strip() in ("1", "true", "yes", "on")
)
_OPPOSITE_SPIKE_LOOKBACK_SEC: float = float(
    os.getenv("DERIV_OPPOSITE_SPIKE_LOOKBACK_SEC", "60") or 60
)


@dataclass
class StructuralExitDecision:
    should_close: bool
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def evaluate_structural_exit(
    contract: Any,
    current_state: dict[str, Any],
    spike_events_recent: list[dict[str, Any]],
    dynamic_config: dict[str, Any],
    market_phase: dict[str, str],
) -> StructuralExitDecision:
    """
    Evalúa si alguna de las 4 condiciones estructurales dispara cierre.

    Args:
        contract: DerivOpenContract (symbol, side, opened_at_ts, peak_profit)
        current_state: snapshot del estado actual del símbolo
        spike_events_recent: lista de spikes desde el log (ts, symbol, direction)
        dynamic_config: {symbol: {is_active, ...}} del orquestador
        market_phase: {symbol: "ACTIVE"|"CAUTION"|"DEAD"} del orquestador

    Returns:
        StructuralExitDecision — should_close=True solo si hay evento estructural.
    """
    if not _STRUCTURAL_EXIT_ENABLED:
        return StructuralExitDecision(False)

    sym: str = getattr(contract, "symbol", "")
    side: str = getattr(contract, "side", "")          # MULTUP | MULTDOWN
    opened_at_ts: float = float(getattr(contract, "opened_at_ts", 0) or 0)

    # ──────────────────────────────────────────────────────────
    # CONDICIÓN 1: SPIKE_OPPOSITE_DETECTED
    # BOOM (MULTUP)  → el spike esperado es UP;  opuesto = DOWN
    # CRASH (MULTDOWN) → el spike esperado es DOWN; opuesto = UP
    # ──────────────────────────────────────────────────────────
    import time as _time
    cutoff_ts = _time.time() - _OPPOSITE_SPIKE_LOOKBACK_SEC
    expected_dir = "UP" if side == "MULTUP" else "DOWN"

    for evt in spike_events_recent:
        if str(evt.get("symbol", "")) != sym:
            continue
        evt_ts = float(evt.get("ts", 0) or 0)
        if evt_ts < opened_at_ts:
            continue  # spike previo a la entrada
        if evt_ts < cutoff_ts:
            continue  # demasiado antiguo
        spike_dir = str(evt.get("direction", "")).upper()
        if spike_dir and spike_dir != expected_dir:
            _LOGGER.info(
                "[STRUCTURAL_EXIT] %s SPIKE_OPPOSITE_DETECTED side=%s expected=%s got=%s ts=%.0f",
                sym, side, expected_dir, spike_dir, evt_ts,
            )
            return StructuralExitDecision(
                should_close=True,
                reason="opposite_spike_detected",
                metadata={
                    "spike_ts": evt_ts,
                    "spike_jump": evt.get("jump"),
                    "expected_dir": expected_dir,
                    "actual_dir": spike_dir,
                },
            )

    # ──────────────────────────────────────────────────────────
    # CONDICIÓN 2: FVG_BOS_OPPOSITE
    # Solo evalúa si tenemos BOS explícito en current_state.
    # Si el dato no está disponible, no cierra (safe default).
    # ──────────────────────────────────────────────────────────
    bos_dir = current_state.get("structural_bos_direction")
    if bos_dir:
        expected_bos = "up" if side == "MULTUP" else "down"
        if str(bos_dir).lower() != expected_bos:
            _LOGGER.info(
                "[STRUCTURAL_EXIT] %s FVG_BOS_OPPOSITE bos=%s expected=%s",
                sym, bos_dir, expected_bos,
            )
            return StructuralExitDecision(
                should_close=True,
                reason="fvg_bos_opposite",
                metadata={"bos_direction": bos_dir, "expected": expected_bos},
            )

    # ──────────────────────────────────────────────────────────
    # CONDICIÓN 3: SYMBOL_DEACTIVATED_BY_ORCHESTRATOR
    # ──────────────────────────────────────────────────────────
    sym_cfg = dynamic_config.get(sym, {})
    if sym_cfg.get("is_active") is False:
        # Respetar bypass explícito de símbolos dinámicamente inactivos
        bypass_raw = os.getenv("DERIV_DYNAMIC_INACTIVE_BYPASS_SYMBOLS", "") or ""
        bypass_set = {s.strip() for s in bypass_raw.split(",") if s.strip()}
        if sym not in bypass_set:
            _LOGGER.info(
                "[STRUCTURAL_EXIT] %s SYMBOL_DEACTIVATED_BY_ORCHESTRATOR is_active=False",
                sym,
            )
            return StructuralExitDecision(
                should_close=True,
                reason="symbol_deactivated_by_orchestrator",
                metadata={"is_active": False},
            )

    # ──────────────────────────────────────────────────────────
    # CONDICIÓN 4: MARKET_PHASE_DEAD
    # ──────────────────────────────────────────────────────────
    phase = market_phase.get(sym, "")
    if str(phase).upper() == "DEAD":
        _LOGGER.info("[STRUCTURAL_EXIT] %s MARKET_PHASE_DEAD phase=%s", sym, phase)
        return StructuralExitDecision(
            should_close=True,
            reason="market_phase_dead",
            metadata={"phase": phase},
        )

    return StructuralExitDecision(False)
