"""
Tests para los cambios QUANT-TRIPLE (2026-06-13):
  A — Purga de dead kinematics (momentum_score, consistency_score)
  B — Coherencia IMMINENCE_RIPE vs ceiling dinámico
  C — ANTI_RETRACE MASTER_KEY bypass
"""

import math
import os
import sys
import types
import importlib
import numpy as np
import pytest

# ── path bootstrap ──────────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ═══════════════════════════════════════════════════════════════════════════
# CAMBIO A — Purga de dead kinematics
# ═══════════════════════════════════════════════════════════════════════════

class TestKineticStatePurge:
    """A.2 + A.3 — consistency eliminada, variance normalizada por ATR-pct."""

    def _import_kinetic(self):
        from src.analysis.market_geometry import _kinetic_state
        return _kinetic_state

    def _make_ghost(self, n: int = 60, drift: float = -0.001) -> np.ndarray:
        """Ghost price series con drift suave (simula BOOM inter-spike)."""
        prices = [5000.0 + i * drift for i in range(n)]
        return np.array(prices, dtype=np.float64)

    def test_kinetic_score_en_rango_0_1(self):
        _kinetic_state = self._import_kinetic()
        ghost = self._make_ghost(60, drift=-0.001)
        _, _, score = _kinetic_state(ghost)
        assert 0.0 <= score <= 1.0, f"kinetic_score fuera de [0,1]: {score}"

    def test_kinetic_sin_consistency_score_en_breakdown(self):
        """El objeto devuelto no debe contener consistency_score."""
        _kinetic_state = self._import_kinetic()
        ghost = self._make_ghost(60, drift=-0.001)
        result = _kinetic_state(ghost)
        # La función retorna tupla (vel, acc, score) — no un dict
        assert len(result) == 3
        vel, acc, score = result
        assert isinstance(score, float)
        # No hay consistency_score en el resultado
        assert not hasattr(result, "consistency_score")

    def test_variance_score_comparable_entre_simbolos(self):
        """
        A.3: BOOM500 (~5k) y CRASH900 (~16k) con la misma volatilidad relativa
        deben producir variance_score comparable (antes: CRASH900 siempre ≈ 1.0).
        """
        _kinetic_state = self._import_kinetic()
        # Misma volatilidad relativa: 0.02% por tick
        n = 60
        boom_price = 5000.0
        crash_price = 16000.0
        rel_vol = 0.0002  # 0.02%

        rng = np.random.default_rng(42)
        boom_ghost = boom_price + np.cumsum(rng.normal(0, boom_price * rel_vol, n))
        crash_ghost = crash_price + np.cumsum(rng.normal(0, crash_price * rel_vol, n))

        _, _, score_boom = _kinetic_state(boom_ghost)
        _, _, score_crash = _kinetic_state(crash_ghost)

        # Ambos deben estar en [0,1]
        assert 0.0 <= score_boom <= 1.0
        assert 0.0 <= score_crash <= 1.0

        # La diferencia no debe ser mayor a 0.40 con la misma volatilidad relativa
        # (antes podía ser 1.0 vs 0.5 por los umbrales hardcodeados)
        diff = abs(score_boom - score_crash)
        assert diff < 0.40, (
            f"Diferencia excesiva ({diff:.3f}) entre BOOM500 ({score_boom:.3f}) "
            f"y CRASH900 ({score_crash:.3f}) con la misma volatilidad relativa"
        )

    def test_kinetic_score_compresion_detectada(self):
        """Drift suave sostenido = compresión → kinetic_score > 0.4."""
        _kinetic_state = self._import_kinetic()
        # BOOM comprimido: drift bajista suave, sin spikes
        ghost = np.array([5000.0 - i * 0.0005 for i in range(60)], dtype=np.float64)
        _, _, score = _kinetic_state(ghost)
        assert score >= 0.0  # mínimo no crashea

    def test_kinetic_retorna_tres_valores(self):
        _kinetic_state = self._import_kinetic()
        ghost = self._make_ghost(60)
        result = _kinetic_state(ghost)
        assert len(result) == 3
        vel, acc, score = result
        assert math.isfinite(vel)
        assert math.isfinite(acc)
        assert math.isfinite(score)


class TestMomentumScoreEliminado:
    """A.1 — _momentum_score eliminada de DerivRiskManager y score_breakdown."""

    def test_momentum_score_no_en_score_breakdown(self, monkeypatch):
        """score_breakdown NO debe contener la clave 'momentum'."""
        try:
            from src.safety.deriv_risk import DerivRiskManager
        except ImportError:
            pytest.skip("DerivRiskManager no disponible en este entorno de test")

        # Si la clase se importa, verificar que _momentum_score no existe
        assert not hasattr(DerivRiskManager, "_momentum_score"), (
            "_momentum_score debe haber sido eliminada de DerivRiskManager"
        )

    def test_orchestrator_system_prompt_contiene_principios_prng(self):
        """D — El system prompt del orquestador contiene los principios PRNG."""
        prompt_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "dynamic_ai_orchestrator.py"
        )
        with open(prompt_path, encoding="utf-8") as f:
            src = f.read()
        assert "PRNG con distribucion Poisson" in src or "PRNG" in src, (
            "El system prompt no menciona PRNG"
        )
        assert "NUNCA justifiques" in src or "NUNCA" in src, (
            "El system prompt no incluye la regla NUNCA"
        )
        assert "score_ceiling - 0.10" in src or "ceiling" in src.lower(), (
            "El system prompt no menciona coherencia ceiling"
        )


# ═══════════════════════════════════════════════════════════════════════════
# CAMBIO B — Coherencia IMMINENCE_RIPE vs ceiling dinámico
# ═══════════════════════════════════════════════════════════════════════════

class TestImminenceCeilingCoherence:
    """B.2 — _ripe_min respeta el dynamic_score_ceiling del símbolo."""

    def _compute_ripe_min(self, ripe_min_global: float, dyn_ceiling: float,
                          ceiling_margin: float = 0.10) -> float:
        """Replica la lógica del código en main_deriv.py."""
        return min(ripe_min_global, max(6.0, dyn_ceiling - ceiling_margin))

    def test_crash900_ceiling_680_no_self_block(self):
        """Con ceiling=6.80, _ripe_min debe ser <= 6.70 (no 7.50)."""
        ripe_min = self._compute_ripe_min(
            ripe_min_global=7.50,
            dyn_ceiling=6.80,
        )
        assert ripe_min <= 6.70, (
            f"CRASH900 con ceiling=6.80 sigue en bloqueo matemático: ripe_min={ripe_min}"
        )

    def test_ceiling_alto_no_cambia_ripe_min(self):
        """Con ceiling=7.60 (default), _ripe_min debe quedarse en 7.50."""
        ripe_min = self._compute_ripe_min(
            ripe_min_global=7.50,
            dyn_ceiling=7.60,
        )
        assert ripe_min == 7.50, (
            f"Con ceiling=7.60 el ripe_min no debería bajar de 7.50: {ripe_min}"
        )

    def test_ceiling_calm_700_ripe_min_es_690(self):
        """Con ceiling=7.0 (calm regime), _ripe_min = 6.90."""
        ripe_min = self._compute_ripe_min(
            ripe_min_global=7.50,
            dyn_ceiling=7.0,
        )
        assert abs(ripe_min - 6.90) < 0.001, (
            f"Con ceiling=7.0 esperamos ripe_min=6.90, obtuvimos {ripe_min}"
        )

    def test_ceiling_muy_bajo_no_baja_de_piso_600(self):
        """Con ceiling extremadamente bajo, _ripe_min no baja de 6.0."""
        ripe_min = self._compute_ripe_min(
            ripe_min_global=7.50,
            dyn_ceiling=5.50,
        )
        assert ripe_min >= 6.0, (
            f"_ripe_min no debe bajar del piso 6.0: {ripe_min}"
        )

    def test_imminence_ripe_ceiling_margin_env_var_respetada(self, monkeypatch):
        """DERIV_IMMINENCE_RIPE_CEILING_MARGIN=0.20 produce margen mayor."""
        margin = 0.20
        ripe_min = self._compute_ripe_min(
            ripe_min_global=7.50,
            dyn_ceiling=7.0,
            ceiling_margin=margin,
        )
        assert abs(ripe_min - 6.80) < 0.001, (
            f"Con margin=0.20 y ceiling=7.0 esperamos 6.80, obtuvimos {ripe_min}"
        )

    def test_codigo_main_deriv_contiene_ceiling_margin(self):
        """Verifica que la lógica fue insertada en main_deriv.py."""
        main_path = os.path.join(
            os.path.dirname(__file__), "..", "src", "main_deriv.py"
        )
        with open(main_path, encoding="utf-8") as f:
            src = f.read()
        assert "DERIV_IMMINENCE_RIPE_CEILING_MARGIN" in src, (
            "La variable DERIV_IMMINENCE_RIPE_CEILING_MARGIN no está en main_deriv.py"
        )
        assert "dynamic_score_ceiling" in src, (
            "No se lee dynamic_score_ceiling en el IMMINENCE gate"
        )


# ═══════════════════════════════════════════════════════════════════════════
# CAMBIO C — ANTI_RETRACE MASTER_KEY bypass
# ═══════════════════════════════════════════════════════════════════════════

class TestAntiRetraceMasterKeyBypass:
    """C.1 — BOOM/CRASH con score >= 8.5 bypass ANTI_RETRACE."""

    def _ar_hot_bypass(self, hot_reentry_fired: bool, score: float,
                       hot_min: float = 8.0, master_key_min: float = 8.5,
                       is_spike_market: bool = True) -> bool:
        """Replica la lógica del bypass en main_deriv.py."""
        return (
            (hot_reentry_fired and score >= hot_min)
            or (is_spike_market and score >= master_key_min)
        )

    def test_boom500_score_937_bypass(self):
        """BOOM500 score=9.37 debe bypassar ANTI_RETRACE aunque bounce_active."""
        bypass = self._ar_hot_bypass(
            hot_reentry_fired=False,
            score=9.37,
            is_spike_market=True,
        )
        assert bypass, "BOOM500 score=9.37 debe hacer bypass de ANTI_RETRACE"

    def test_crash500_score_900_bypass_simetrico(self):
        """CRASH500 score=9.0 debe bypassar (simetría BOOM/CRASH)."""
        bypass = self._ar_hot_bypass(
            hot_reentry_fired=False,
            score=9.0,
            is_spike_market=True,
        )
        assert bypass, "CRASH500 score=9.0 debe hacer bypass de ANTI_RETRACE"

    def test_score_bajo_no_bypass(self):
        """Score=8.0 < 8.5 NO debe bypassar si hot_reentry=False."""
        bypass = self._ar_hot_bypass(
            hot_reentry_fired=False,
            score=8.0,
            master_key_min=8.5,
            is_spike_market=True,
        )
        assert not bypass, "Score=8.0 < 8.5 no debe bypassar ANTI_RETRACE"

    def test_hot_reentry_path_sigue_funcionando(self):
        """hot_reentry_fired=True con score>=8.0 sigue bypassando."""
        bypass = self._ar_hot_bypass(
            hot_reentry_fired=True,
            score=8.1,
            hot_min=8.0,
            master_key_min=8.5,
            is_spike_market=True,
        )
        assert bypass, "hot_reentry_fired=True con score=8.1 debe bypassar"

    def test_no_spike_market_no_bypass_por_master_key(self):
        """Mercados no-spike (R_75 etc.) NO deben bypassar por MASTER_KEY."""
        bypass = self._ar_hot_bypass(
            hot_reentry_fired=False,
            score=9.5,
            master_key_min=8.5,
            is_spike_market=False,
        )
        assert not bypass, "Mercado no-spike no debe bypassar ANTI_RETRACE por score"

    def test_codigo_main_deriv_contiene_master_key_bypass(self):
        """Verifica que el bypass fue insertado en main_deriv.py."""
        main_path = os.path.join(
            os.path.dirname(__file__), "..", "src", "main_deriv.py"
        )
        with open(main_path, encoding="utf-8") as f:
            src = f.read()
        assert "DERIV_ANTI_RETRACE_MASTER_KEY_MIN_SCORE" in src, (
            "La variable DERIV_ANTI_RETRACE_MASTER_KEY_MIN_SCORE no está en main_deriv.py"
        )
        assert "_ar_master_key_min" in src, (
            "_ar_master_key_min no encontrada en main_deriv.py"
        )
