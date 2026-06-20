"""
D.6.5 — Post-Racha Cooldown Gradual (Diego, 2026-06-20)

Bloquea entradas del bot durante ventana post-racha de spikes perdidos.

Filosofía: cuando el mercado descarga energía en una racha de spikes y
el bot no la cazó, esperar a que el ciclo se reinicie antes de entrar.

Análisis que lo sustenta (332 trades, 9.5h post-D.6.3):
  - WR post-racha entrada 120-300s: 14-37%
  - WR post-racha entrada >=300s:   67%
  - WR global baseline:             53.3%

Cooldown gradual (decisión Diego 2026-06-20):
  2 spikes → 240s (4 min)
  3 spikes → 300s (5 min)
  4 spikes → 420s (7 min)
  5+ spikes → 600s (10 min)

Si llega un spike DURANTE el cooldown y el bot está fuera → se suma
a la racha y el cooldown se re-inicia con la nueva duración.
"""

import json
import os
import time
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_LOGGER = logging.getLogger("deriv.daemon")

_STATE_DIR = os.getenv("DERIV_STATE_DIR") or os.getenv("BOT_STATE_DIR") or "/data/logs"
_COOLDOWN_STATE_FILE = os.path.join(_STATE_DIR, "d6_cooldown_state.json")

_ENABLED = (
    os.getenv("DERIV_D6_POST_RACHA_COOLDOWN_ENABLED", "true")
    .strip().lower() in {"1", "true", "yes", "on"}
)
_CD_2    = max(60,  int(os.getenv("DERIV_D6_PR_COOLDOWN_2_SPIKES_SEC",    "240") or 240))
_CD_3    = max(60,  int(os.getenv("DERIV_D6_PR_COOLDOWN_3_SPIKES_SEC",    "300") or 300))
_CD_4    = max(60,  int(os.getenv("DERIV_D6_PR_COOLDOWN_4_SPIKES_SEC",    "420") or 420))
_CD_5P   = max(120, int(os.getenv("DERIV_D6_PR_COOLDOWN_5PLUS_SPIKES_SEC","600") or 600))
_GAP_P50_FALLBACK = max(60, int(os.getenv("DERIV_D6_PR_GAP_P50_FALLBACK_SEC", "400") or 400))


def _cooldown_for(spike_count: int) -> int:
    if spike_count >= 5:
        return _CD_5P
    if spike_count == 4:
        return _CD_4
    if spike_count == 3:
        return _CD_3
    return _CD_2


@dataclass
class _CooldownState:
    active: bool = False
    until_ts: float = 0.0
    spike_count: int = 0
    started_ts: float = 0.0


class PostRachaCooldownTracker:
    """
    Detecta rachas de spikes perdidos y aplica cooldown gradual por símbolo.

    Uso:
        tracker.record_spike(symbol, spike_ts, bot_in_position)
        if tracker.is_in_cooldown(symbol):
            # bloquear ghost ALLOW
        info = tracker.get_cooldown_info(symbol)  # para el panel
    """

    def __init__(self) -> None:
        # Buffer de hasta 50 spike timestamps por símbolo (para gap_p50)
        self._spike_buf: dict[str, deque] = defaultdict(lambda: deque(maxlen=50))
        # Último ts de spike visto (para evitar re-procesar el mismo spike en cada tick)
        self._last_seen_ts: dict[str, float] = defaultdict(float)
        # Estado de cooldown activo
        self._state: dict[str, _CooldownState] = defaultdict(_CooldownState)
        # Cache de gap_p50 por símbolo (se invalida al agregar spike)
        self._gap_p50_cache: dict[str, float] = {}
        # Estado de posición por símbolo (para detectar cierre)
        self._in_trade: dict[str, bool] = defaultdict(bool)
        # Spikes que llegaron DURANTE una posición abierta
        self._spikes_in_trade: dict[str, int] = defaultdict(int)

        _LOGGER.info(
            "[D6_PR_INIT] enabled=%s | cd: 2=%ds 3=%ds 4=%ds 5+=%ds gap_fallback=%ds",
            _ENABLED, _CD_2, _CD_3, _CD_4, _CD_5P, _GAP_P50_FALLBACK,
        )

    def _get_gap_p50(self, symbol: str) -> float:
        if symbol in self._gap_p50_cache:
            return self._gap_p50_cache[symbol]
        buf = self._spike_buf[symbol]
        if len(buf) < 4:
            return float(_GAP_P50_FALLBACK)
        times = sorted(buf)
        gaps = sorted(times[i] - times[i - 1] for i in range(1, len(times)))
        p50 = gaps[len(gaps) // 2]
        self._gap_p50_cache[symbol] = p50
        return p50

    def record_spike(self, symbol: str, spike_ts: float, bot_in_position: bool) -> None:
        """
        Llamar cuando se detecta un nuevo spike para el símbolo.

        bot_in_position: True si el bot tenía contrato abierto en ese símbolo
        durante el spike (el bot "cazó" el spike).
        """
        if not _ENABLED:
            return
        # Idempotente: ignorar si ya procesamos este spike
        if spike_ts <= self._last_seen_ts[symbol]:
            return
        self._last_seen_ts[symbol] = spike_ts

        # Agregar al buffer e invalidar cache gap_p50
        self._spike_buf[symbol].append(spike_ts)
        self._gap_p50_cache.pop(symbol, None)

        state = self._state[symbol]
        gap_p50 = self._get_gap_p50(symbol)

        # ── Spike durante posición abierta ────────────────────────────────
        if bot_in_position:
            # Contar para el cooldown post-trade
            self._spikes_in_trade[symbol] += 1
            # Si había cooldown activo → terminarlo (bot cazó el spike)
            if state.active and spike_ts <= state.until_ts:
                state.active = False
                state.until_ts = 0.0
                _LOGGER.info(
                    "[D6_PR_COOLDOWN_END] %s | bot en posición → cooldown cancelado"
                    " (spikes_in_trade=%d)",
                    symbol, self._spikes_in_trade[symbol],
                )
                self._flush()
            return  # spikes durante trade no contribuyen a racha de spikes perdidos

        # ── Cooldown activo + spike perdido ────────────────────────────────
        if state.active and spike_ts <= state.until_ts:
            # Bot perdió otro spike → extender racha y re-iniciar cooldown
            state.spike_count += 1
            new_cd = _cooldown_for(state.spike_count)
            state.until_ts = spike_ts + new_cd
            _LOGGER.info(
                "[D6_PR_COOLDOWN_EXTEND] %s | spike #%d perdido durante cooldown"
                " → nuevo cooldown=%ds (total=%ds)",
                symbol, state.spike_count, new_cd,
                int(state.until_ts - state.started_ts),
            )
            self._flush()
            return

        # ── Cooldown NO activo → detectar racha de spikes perdidos ────────

        # Contar cuántos spikes consecutivos RECIENTES tienen gaps < gap_p50
        buf_sorted = sorted(self._spike_buf[symbol])
        streak_count = 1
        for i in range(len(buf_sorted) - 2, -1, -1):
            g = buf_sorted[i + 1] - buf_sorted[i]
            if g < gap_p50:
                streak_count += 1
            else:
                break

        if streak_count >= 3:
            cd_dur = _cooldown_for(streak_count)
            state.active = True
            state.spike_count = streak_count
            state.started_ts = spike_ts
            state.until_ts = spike_ts + cd_dur
            _LOGGER.info(
                "[D6_PR_COOLDOWN_ACTIVATE] %s | racha=%d spikes (gap_p50=%.0fs)"
                " → cooldown=%ds",
                symbol, streak_count, gap_p50, cd_dur,
            )
            self._flush()

    def is_in_cooldown(self, symbol: str) -> bool:
        """True si el símbolo está en cooldown ahora mismo."""
        if not _ENABLED:
            return False
        state = self._state[symbol]
        if not state.active:
            return False
        now = time.time()
        if now >= state.until_ts:
            state.active = False
            _LOGGER.info(
                "[D6_PR_COOLDOWN_EXPIRED] %s | cooldown terminado por tiempo"
                " (duró %.0fs, %d spikes en racha)",
                symbol,
                now - state.started_ts,
                state.spike_count,
            )
            self._flush()
            return False
        return True

    def get_cooldown_info(self, symbol: str) -> Optional[dict]:
        """Info del cooldown para panel/API. None si no hay cooldown activo."""
        if not self.is_in_cooldown(symbol):
            return None
        state = self._state[symbol]
        now = time.time()
        remaining = max(0, state.until_ts - now)
        return {
            "active": True,
            "spike_count": state.spike_count,
            "remaining_seconds": int(remaining),
            "until_ts": round(state.until_ts, 3),
            "started_ts": round(state.started_ts, 3),
        }

    def notify_position_state(self, symbol: str, in_position: bool) -> None:
        """
        Llamar cada tick con el estado de posición actual del símbolo.

        Si el bot estaba en posición y la cierra, NO activa cooldown — el bot
        capturó los spikes durante el trade. El ghost puede re-evaluar libremente.
        Solo hay cooldown si estamos FUERA y pasan ≥3 spikes consecutivos perdidos.
        """
        if not _ENABLED:
            return
        was_in = self._in_trade[symbol]
        self._in_trade[symbol] = in_position

        if not in_position and was_in:
            # Posición cerrada → reset contador, sin cooldown post-trade
            self._spikes_in_trade[symbol] = 0

        elif in_position and not was_in:
            # Nueva posición abierta → reset contador de spikes durante trade
            self._spikes_in_trade[symbol] = 0

    def force_clear(self, symbol: str | None = None) -> None:
        """Limpiar cooldown. symbol=None limpia todos."""
        if symbol:
            self._state[symbol] = _CooldownState()
            self._last_seen_ts[symbol] = 0.0
            self._spikes_in_trade[symbol] = 0
            self._in_trade[symbol] = False
            _LOGGER.info("[D6_PR_FORCE_CLEAR] %s", symbol)
        else:
            self._state.clear()
            self._last_seen_ts.clear()
            self._spikes_in_trade.clear()
            self._in_trade.clear()
            _LOGGER.info("[D6_PR_FORCE_CLEAR_ALL]")
        self._flush()

    def _flush(self) -> None:
        """Escribir estado completo de todos los símbolos al archivo JSON."""
        try:
            now = time.time()
            payload: dict = {}
            for sym, state in self._state.items():
                if state.active and now < state.until_ts:
                    payload[sym] = {
                        "active": True,
                        "spike_count": state.spike_count,
                        "remaining_seconds": int(state.until_ts - now),
                        "until_ts": round(state.until_ts, 3),
                        "started_ts": round(state.started_ts, 3),
                    }
                else:
                    payload[sym] = {"active": False}
            Path(_COOLDOWN_STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
            with open(_COOLDOWN_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"updated_at": now, "symbols": payload}, f)
        except Exception as exc:
            _LOGGER.warning("[D6_PR_FLUSH_ERR] %s", exc)


# Singleton global
POST_RACHA_COOLDOWN = PostRachaCooldownTracker()
