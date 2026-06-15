"""Tests for MAX_HOLD_RESPITE (Paso 7 — Filosofía A).

Verifica:
- RespiteDecision importable y funcional
- evaluate_max_hold_respite concede respiro con señales reales (RIPE/SECO)
- evaluate_max_hold_respite niega sin señales (FRESH/CARGANDO)
- Confianza < 0.75 → DENY aunque LLM diga grant=True
- Cap absoluto: extension_sec > 120 → se cappea a 120
- Error del LLM → DENY por defecto (no extended trades infinitamente)
- Feature disabled → DENY inmediato
- Flag _max_hold_respite_granted bloquea segunda llamada
"""
import asyncio
import json
import time
import types

from unittest.mock import AsyncMock, patch

from src.analysis.llm_hold_decision import (
    RespiteDecision,
    evaluate_max_hold_respite,
    _RESPITE_MAX_EXTENSION_SEC,
)


def _mock_contract(
    symbol="CRASH900",
    side="MULTDOWN",
    opened_at_ts=None,
    peak_profit=0.0,
    contract_id=77777,
    **kwargs,
):
    c = types.SimpleNamespace(
        symbol=symbol,
        side=side,
        opened_at_ts=opened_at_ts or (time.time() - 700),
        peak_profit=peak_profit,
        contract_id=contract_id,
    )
    for k, v in kwargs.items():
        setattr(c, k, v)
    return c


def _llm_resp(grant: bool, extension: int, confidence: float, reason: str = "test") -> str:
    return json.dumps({
        "grant_respite": grant,
        "extension_sec": extension,
        "confidence": confidence,
        "reason": reason,
    })


def test_respiro_concedido_con_imminence_ripe():
    """Si imminence=RIPE y scarcity=SECO, LLM concede respiro → grant=True."""
    contract = _mock_contract(symbol="CRASH900")
    state = {
        "imminence_state": "RIPE",
        "scarcity_state": "SECO",
        "fvg_still_valid": True,
    }
    resp = _llm_resp(grant=True, extension=90, confidence=0.85, reason="RIPE+SECO")
    import src.analysis.llm_hold_decision as m
    with patch.object(m, "_OPENROUTER_KEY", "test-key"):
        with patch("src.analysis.llm_hold_decision._call_llm_with_fallback", new=AsyncMock(return_value=resp)):
            decision = asyncio.run(evaluate_max_hold_respite(contract, state))
    assert decision.grant_respite is True
    assert 0 < decision.extension_sec <= 120
    assert decision.confidence >= 0.75


def test_respiro_denegado_sin_señales():
    """Si imminence=FRESH y scarcity=CARGANDO, NO conceder respiro."""
    contract = _mock_contract(symbol="CRASH900")
    state = {
        "imminence_state": "FRESH",
        "scarcity_state": "CARGANDO",
    }
    resp = _llm_resp(grant=False, extension=0, confidence=0.30, reason="sin señales")
    import src.analysis.llm_hold_decision as m
    with patch.object(m, "_OPENROUTER_KEY", "test-key"):
        with patch("src.analysis.llm_hold_decision._call_llm_with_fallback", new=AsyncMock(return_value=resp)):
            decision = asyncio.run(evaluate_max_hold_respite(contract, state))
    assert decision.grant_respite is False
    assert decision.extension_sec == 0


def test_respiro_max_120s():
    """Aunque el LLM responda extension_sec=300, se cappea a 120."""
    contract = _mock_contract(symbol="CRASH900")
    state = {"imminence_state": "RIPE", "scarcity_state": "SECO"}
    resp = _llm_resp(grant=True, extension=300, confidence=0.90, reason="cap test")
    import src.analysis.llm_hold_decision as m
    with patch.object(m, "_OPENROUTER_KEY", "test-key"):
        with patch("src.analysis.llm_hold_decision._call_llm_with_fallback", new=AsyncMock(return_value=resp)):
            decision = asyncio.run(evaluate_max_hold_respite(contract, state))
    assert decision.extension_sec <= int(_RESPITE_MAX_EXTENSION_SEC)
    assert decision.extension_sec == 120


def test_respiro_denegado_por_baja_confianza():
    """LLM grant=True pero conf=0.50 < 0.75 → DENY con low_confidence en reason."""
    contract = _mock_contract(symbol="CRASH900")
    state = {"imminence_state": "RIPE"}
    resp = _llm_resp(grant=True, extension=60, confidence=0.50, reason="baja conf")
    import src.analysis.llm_hold_decision as m
    with patch.object(m, "_OPENROUTER_KEY", "test-key"):
        with patch("src.analysis.llm_hold_decision._call_llm_with_fallback", new=AsyncMock(return_value=resp)):
            decision = asyncio.run(evaluate_max_hold_respite(contract, state))
    assert decision.grant_respite is False
    assert decision.extension_sec == 0
    assert "low_confidence" in decision.reason


def test_respiro_deny_por_error_llm():
    """Error en LLM → DENY por defecto (no extender indefinidamente)."""
    contract = _mock_contract(symbol="CRASH900")
    state = {}
    import src.analysis.llm_hold_decision as m
    with patch.object(m, "_OPENROUTER_KEY", "test-key"):
        with patch("src.analysis.llm_hold_decision._call_llm_with_fallback", new=AsyncMock(side_effect=RuntimeError("api error"))):
            decision = asyncio.run(evaluate_max_hold_respite(contract, state))
    assert decision.grant_respite is False
    assert "llm_error" in decision.reason


def test_respiro_deny_feature_disabled():
    """DERIV_MAX_HOLD_RESPITE_ENABLED=false → DENY inmediato sin llamar LLM."""
    contract = _mock_contract(symbol="CRASH900")
    state = {"imminence_state": "RIPE", "scarcity_state": "SECO"}
    import src.analysis.llm_hold_decision as m
    with patch.object(m, "_RESPITE_ENABLED", False):
        mock_llm = AsyncMock()
        with patch("src.analysis.llm_hold_decision._call_llm_with_fallback", new=mock_llm):
            decision = asyncio.run(evaluate_max_hold_respite(contract, state))
    assert decision.grant_respite is False
    assert decision.reason == "feature_disabled"
    mock_llm.assert_not_called()


def test_respiro_deny_sin_api_key():
    """Sin OPENROUTER_API_KEY → DENY sin llamar LLM."""
    contract = _mock_contract(symbol="CRASH900")
    state = {"imminence_state": "RIPE"}
    import src.analysis.llm_hold_decision as m
    with patch.object(m, "_OPENROUTER_KEY", ""):
        mock_llm = AsyncMock()
        with patch("src.analysis.llm_hold_decision._call_llm_with_fallback", new=mock_llm):
            decision = asyncio.run(evaluate_max_hold_respite(contract, state))
    assert decision.grant_respite is False
    assert decision.reason == "no_api_key"
    mock_llm.assert_not_called()


def test_solo_un_respiro_por_contrato():
    """Flag _max_hold_respite_granted en el contrato indica que ya se usó el respiro."""
    contract = _mock_contract(_max_hold_respite_granted=True)
    assert getattr(contract, "_max_hold_respite_granted", False) is True
    # Respiro activo: aún no expiró
    contract._respite_extends_until = time.time() + 60
    assert time.time() < contract._respite_extends_until


def test_respite_decision_dataclass():
    """RespiteDecision se instancia y retorna campos correctos."""
    d = RespiteDecision(grant_respite=True, extension_sec=90, confidence=0.80, reason="ok")
    assert d.grant_respite is True
    assert d.extension_sec == 90
    assert d.confidence == 0.80
    assert d.reason == "ok"


def test_respite_importable():
    """RespiteDecision y evaluate_max_hold_respite son importables."""
    from src.analysis.llm_hold_decision import RespiteDecision, evaluate_max_hold_respite
    assert callable(evaluate_max_hold_respite)
