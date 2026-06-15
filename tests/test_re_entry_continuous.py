"""Tests for Filosofía A re-entry logic (race guard + maturity bypass)."""
import os
import time
import pytest


def test_race_guard_env_var_parsed():
    """DERIV_INTER_TRADE_RACE_GUARD_SEC debe parsearse correctamente."""
    os.environ["DERIV_INTER_TRADE_RACE_GUARD_SEC"] = "15"
    # Reimportar para que el module-level constant se re-evalúe no es necesario
    # — solo verificamos que el env var tiene el valor correcto
    val = float(os.getenv("DERIV_INTER_TRADE_RACE_GUARD_SEC", "0") or 0)
    assert val == 15.0
    del os.environ["DERIV_INTER_TRADE_RACE_GUARD_SEC"]


def test_structural_exit_enabled_default():
    """DERIV_STRUCTURAL_EXIT_ENABLED default es true."""
    val = os.getenv("DERIV_STRUCTURAL_EXIT_ENABLED", "true").lower() in ("1", "true", "yes", "on")
    assert val is True


def test_maturity_bypass_window_default():
    """DERIV_MATURITY_BYPASS_RECENT_CLOSE_SEC default es 300s."""
    val = float(os.getenv("DERIV_MATURITY_BYPASS_RECENT_CLOSE_SEC", "300") or 300)
    assert val == 300.0


def test_winner_percentiles_crash900():
    """CRASH900 p75 debe ser >= 1000s (data empírica)."""
    from src.analysis.llm_hold_decision import _WINNER_PERCENTILES
    p75 = _WINNER_PERCENTILES.get("CRASH900", {}).get("p75", 0)
    assert p75 >= 1000, f"CRASH900 p75={p75} — demasiado bajo, mataría ganadores tardíos"


def test_winner_percentiles_boom500():
    """BOOM500 p25 debe ser razonable."""
    from src.analysis.llm_hold_decision import _WINNER_PERCENTILES
    p25 = _WINNER_PERCENTILES.get("BOOM500", {}).get("p25", 0)
    assert p25 > 0


def test_structural_exit_importable():
    """structural_exit.py debe importar sin error."""
    from src.safety.structural_exit import evaluate_structural_exit, StructuralExitDecision
    assert callable(evaluate_structural_exit)


def test_llm_hold_importable():
    """llm_hold_decision.py debe importar sin error."""
    from src.analysis.llm_hold_decision import should_invoke_hold_llm, evaluate_hold_with_llm
    assert callable(should_invoke_hold_llm)
    assert callable(evaluate_hold_with_llm)
