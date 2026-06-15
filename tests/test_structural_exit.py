"""Tests for Filosofía A structural exit logic."""
import time
import types
import pytest

from src.safety.structural_exit import evaluate_structural_exit, StructuralExitDecision


def _mock_contract(
    symbol="BOOM500",
    side="MULTUP",
    opened_at_ts=None,
    peak_profit=0.0,
):
    c = types.SimpleNamespace(
        symbol=symbol,
        side=side,
        opened_at_ts=opened_at_ts or (time.time() - 60),
        peak_profit=peak_profit,
    )
    return c


def test_no_cierra_por_score_bajo():
    """Score/RNG actuales bajos NO deben cerrar — drift normal del PRNG."""
    contract = _mock_contract()
    state = {"current_score": 3.5}  # cayó mucho desde la entrada
    result = evaluate_structural_exit(contract, state, [], {}, {})
    assert result.should_close is False


def test_no_cierra_por_tiempo():
    """Solo tiempo transcurrido NO es razón estructural."""
    contract = _mock_contract(opened_at_ts=time.time() - 700)  # 700s
    result = evaluate_structural_exit(contract, {}, [], {}, {})
    assert result.should_close is False


def test_cierra_por_spike_opuesto_boom():
    """Spike DOWN en BOOM500 con posición MULTUP → cerrar."""
    contract = _mock_contract(symbol="BOOM500", side="MULTUP")
    spikes = [{"symbol": "BOOM500", "direction": "DOWN", "ts": time.time() - 10, "jump": 5.0}]
    result = evaluate_structural_exit(contract, {}, spikes, {}, {})
    assert result.should_close is True
    assert result.reason == "opposite_spike_detected"


def test_cierra_por_spike_opuesto_crash():
    """Spike UP en CRASH500 con posición MULTDOWN → cerrar."""
    contract = _mock_contract(symbol="CRASH500", side="MULTDOWN")
    spikes = [{"symbol": "CRASH500", "direction": "UP", "ts": time.time() - 5, "jump": 4.0}]
    result = evaluate_structural_exit(contract, {}, spikes, {}, {})
    assert result.should_close is True
    assert result.reason == "opposite_spike_detected"


def test_no_cierra_spike_esperado():
    """Spike en la dirección esperada NO debe cerrar."""
    contract = _mock_contract(symbol="BOOM500", side="MULTUP")
    spikes = [{"symbol": "BOOM500", "direction": "UP", "ts": time.time() - 5, "jump": 6.0}]
    result = evaluate_structural_exit(contract, {}, spikes, {}, {})
    assert result.should_close is False


def test_no_cierra_spike_previo_a_entrada():
    """Spike opuesto ANTES de la apertura no debe cerrar."""
    opened_ts = time.time() - 30
    contract = _mock_contract(symbol="BOOM500", side="MULTUP", opened_at_ts=opened_ts)
    spikes = [{"symbol": "BOOM500", "direction": "DOWN", "ts": opened_ts - 10}]
    result = evaluate_structural_exit(contract, {}, spikes, {}, {})
    assert result.should_close is False


def test_no_cierra_spike_otro_simbolo():
    """Spike opuesto en OTRO símbolo no debe cerrar esta posición."""
    contract = _mock_contract(symbol="BOOM500", side="MULTUP")
    spikes = [{"symbol": "CRASH500", "direction": "DOWN", "ts": time.time() - 5}]
    result = evaluate_structural_exit(contract, {}, spikes, {}, {})
    assert result.should_close is False


def test_cierra_por_symbol_deactivated():
    """Símbolo desactivado por orquestador → cerrar."""
    contract = _mock_contract(symbol="CRASH900")
    dynamic_config = {"CRASH900": {"is_active": False}}
    result = evaluate_structural_exit(contract, {}, [], dynamic_config, {})
    assert result.should_close is True
    assert result.reason == "symbol_deactivated_by_orchestrator"


def test_cierra_por_market_phase_dead():
    """market_phase=DEAD → cerrar."""
    contract = _mock_contract(symbol="CRASH900")
    result = evaluate_structural_exit(contract, {}, [], {}, {"CRASH900": "DEAD"})
    assert result.should_close is True
    assert result.reason == "market_phase_dead"


def test_no_cierra_market_phase_active():
    """market_phase=ACTIVE → no cerrar."""
    contract = _mock_contract(symbol="CRASH900")
    result = evaluate_structural_exit(contract, {}, [], {}, {"CRASH900": "ACTIVE"})
    assert result.should_close is False


def test_no_cierra_market_phase_caution():
    """market_phase=CAUTION no cierra solo (el LLM evalúa)."""
    contract = _mock_contract(symbol="CRASH900")
    result = evaluate_structural_exit(contract, {}, [], {}, {"CRASH900": "CAUTION"})
    assert result.should_close is False


def test_retorna_dataclass_correcto():
    """El resultado debe ser StructuralExitDecision con todos los campos."""
    contract = _mock_contract()
    result = evaluate_structural_exit(contract, {}, [], {}, {})
    assert isinstance(result, StructuralExitDecision)
    assert hasattr(result, "should_close")
    assert hasattr(result, "reason")
    assert hasattr(result, "metadata")
