"""Tests for LLM Hold Decision rate limiting and trigger logic."""
import time
import types
import pytest

from src.analysis.llm_hold_decision import (
    should_invoke_hold_llm,
    _WINNER_PERCENTILES,
    _last_call_ts,
)


def _mock_contract(
    symbol="CRASH900",
    peak_profit=0.0,
    opened_at_ts=None,
    contract_id=99999,
):
    c = types.SimpleNamespace(
        symbol=symbol,
        side="MULTDOWN",
        peak_profit=peak_profit,
        opened_at_ts=opened_at_ts or (time.time() - 100),
        contract_id=contract_id,
        floating_pnl=-0.5,
        max_hold_seconds=600.0,
        score_breakdown={},
    )
    return c


def test_no_invoca_si_held_corto():
    """Si held_sec < 80% p75 CRASH900 (1310*0.8=1048s), no invocar LLM."""
    contract = _mock_contract(symbol="CRASH900", peak_profit=0.0)
    # held_sec = 100 → muy por debajo de 1048s
    result = should_invoke_hold_llm(contract, 100, {})
    assert result is False


def test_invoca_si_held_largo_sin_peak():
    """Si held > 80% p75 y peak=0 → invocar LLM."""
    p75 = _WINNER_PERCENTILES["CRASH900"]["p75"]  # 1310
    threshold = p75 * 0.80  # 1048
    contract = _mock_contract(symbol="CRASH900", peak_profit=0.0)
    result = should_invoke_hold_llm(contract, threshold + 10, {})
    assert result is True


def test_no_invoca_si_held_largo_con_peak():
    """Si held largo pero peak > 0, no hay trigger por hold largo."""
    p75 = _WINNER_PERCENTILES["CRASH900"]["p75"]
    contract = _mock_contract(symbol="CRASH900", peak_profit=0.5)
    result = should_invoke_hold_llm(contract, p75 * 0.90, {})
    # No trigger por hold largo si hay peak (spike en camino)
    assert result is False


def test_rate_limit_funciona():
    """No invocar LLM dos veces en < MIN_INTERVAL_SEC."""
    cid = 88888
    contract = _mock_contract(symbol="CRASH900", peak_profit=0.0, contract_id=cid)
    p75 = _WINNER_PERCENTILES["CRASH900"]["p75"]
    # Simular llamada reciente (hace 30s)
    _last_call_ts[cid] = time.time() - 30
    result = should_invoke_hold_llm(contract, p75 * 0.90, {})
    assert result is False  # rate limited
    del _last_call_ts[cid]


def test_invoca_si_market_phase_caution():
    """market_phase=CAUTION es trigger independiente del hold."""
    contract = _mock_contract(symbol="BOOM500", peak_profit=0.0)
    result = should_invoke_hold_llm(contract, 10, {"market_phase": "CAUTION"})
    assert result is True


def test_invoca_si_structural_ambiguous():
    """structural_ambiguous=True es trigger explícito."""
    contract = _mock_contract(symbol="BOOM500", peak_profit=0.0)
    result = should_invoke_hold_llm(contract, 10, {"structural_ambiguous": True})
    assert result is True


def test_no_invoca_sin_contract_id():
    """Si no hay contract_id, no invocar (no hay forma de rate limit)."""
    contract = types.SimpleNamespace(
        symbol="CRASH900",
        peak_profit=0.0,
        contract_id=None,
    )
    result = should_invoke_hold_llm(contract, 2000, {})
    assert result is False
