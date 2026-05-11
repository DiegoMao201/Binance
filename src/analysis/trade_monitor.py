"""Dynamic AI Trade Monitor — monitoreo activo de posiciones abiertas.

Cada vez que run_cycle() detecta una posicion abierta, este modulo:
1. Recopila el estado actual de la posicion y del mercado (PnL, RSI, volumen,
   orderbook, trade flow, tiempo abierto).
2. Recupera los ultimos 3 veredictos previos de la IA (memoria estateful
   persistida en logs/trade_monitor_log.json).
3. Construye un prompt enriquecido y lo envia a OpenRouter (GPT-4o-mini).
4. Parsea la respuesta JSON y devuelve una accion: HOLD | UPDATE_SL | EMERGENCY_CLOSE.
5. Persiste el veredicto para auditoria en el dashboard.

Acciones posibles:
  HOLD           -> No hacer nada; el trade sigue activo con buen momentum.
  UPDATE_SL      -> Mover el Stop Loss a entry_price + 0.25% (cubrir fees + micro-profit).
  EMERGENCY_CLOSE -> Cerrar a mercado inmediatamente.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from src.data.binance_client import BinanceClientError, BinanceDataClient
from src.utils.config import Settings
from src.utils.state_store import load_history, persist_history

_MONITOR_MEMORY_WINDOW = 3      # veredictos previos que se envian a la IA
_MONITOR_LOG_RETENTION = 200    # maximo de entradas en el log persistido


class ActiveTradeMonitor:
    """Evalua posiciones abiertas con IA para maximizar ganancias y proteger capital.

    Diseño: stateless en memoria; el estado persiste en trade_monitor_log.json.
    Instanciar una vez por ciclo es seguro.
    """

    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self._log_file: Path = settings.logs_dir / "trade_monitor_log.json"

    # ─────────────────────────────────────────────────────────────────────────
    # MEMORIA ESTATEFUL
    # ─────────────────────────────────────────────────────────────────────────

    def _load_recent_verdicts(self, symbol: str, n: int = _MONITOR_MEMORY_WINDOW) -> list[dict[str, Any]]:
        """Carga los ultimos `n` veredictos de la IA para el simbolo dado."""
        try:
            all_log: list[dict[str, Any]] = load_history(self._log_file)
            symbol_log = [e for e in all_log if e.get("symbol") == symbol]
            return symbol_log[-n:]
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("TradeMonitor: no se pudo leer historial (%s). Continuando sin memoria.", exc)
            return []

    def _persist_verdict(self, verdict: dict[str, Any]) -> None:
        """Agrega el veredicto al log persistente y trunca a _MONITOR_LOG_RETENTION."""
        try:
            all_log: list[dict[str, Any]] = load_history(self._log_file)
            all_log.append(verdict)
            if len(all_log) > _MONITOR_LOG_RETENTION:
                all_log = all_log[-_MONITOR_LOG_RETENTION:]
            persist_history(self._log_file, all_log)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("TradeMonitor: no se pudo persistir veredicto: %s", exc)

    # ─────────────────────────────────────────────────────────────────────────
    # RECOLECCION DE DATOS
    # ─────────────────────────────────────────────────────────────────────────

    def _collect_position_state(
        self,
        position: dict[str, Any],
        scan_result: dict[str, Any] | None,
        client: BinanceDataClient,
    ) -> dict[str, Any]:
        """Recopila estado completo de la posicion y microestructura de mercado."""
        symbol = str(position.get("symbol") or "")
        entry_price = float(position.get("entry_price") or 0.0)
        mark_price = float(position.get("mark_price") or entry_price)
        stop_loss = float(position.get("stop_loss") or 0.0)
        take_profit = float(position.get("take_profit") or 0.0)
        amount = float(position.get("amount") or 0.0)
        trailing_tier = int(position.get("trailing_tier") or 0)

        # PnL flotante
        unrealized_usdt = round((mark_price - entry_price) * amount, 4)
        unrealized_pct = round(
            (mark_price - entry_price) / entry_price * 100, 4
        ) if entry_price > 0 else 0.0
        mfe_pct = round(float(position.get("mfe_pct") or 0.0) * 100, 4)
        mae_pct = round(float(position.get("mae_pct") or 0.0) * 100, 4)

        # Tiempo abierto
        hold_minutes: float | None = None
        opened_at_raw = position.get("opened_at")
        if opened_at_raw:
            try:
                opened_at = datetime.fromisoformat(str(opened_at_raw).replace("Z", "+00:00"))
                hold_minutes = round(
                    (datetime.now(timezone.utc) - opened_at).total_seconds() / 60.0, 1
                )
            except ValueError:
                pass

        # Datos tecnicos del ultimo scan (RSI, volumen, ATR)
        ts = (scan_result or {}).get("technical_signal") or {}
        rsi = round(float(ts.get("rsi") or 50.0), 2)
        volume_ratio = round(float(ts.get("volume_ratio") or 0.0), 3)
        atr_pct_raw = float(ts.get("atr_pct") or 0.0)
        atr_pct = round(atr_pct_raw * 100, 4)

        # Microestructura en vivo (fresh call por posicion, no del cache del scan)
        orderbook_imbalance = 0.5
        trade_flow_score = 0.5
        tape_momentum_pct = 0.0

        try:
            ob = client.fetch_orderbook_snapshot(symbol=symbol, depth=20)
            if "imbalance" in ob:
                orderbook_imbalance = round(float(ob["imbalance"]), 4)
        except BinanceClientError as exc:
            self.logger.warning("TradeMonitor: orderbook no disponible para %s: %s", symbol, exc)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("TradeMonitor: orderbook error para %s: %s", symbol, exc)

        try:
            tf = client.fetch_trade_flow_snapshot(symbol=symbol, limit=80)
            if "flow_score" in tf:
                trade_flow_score = round(float(tf["flow_score"]), 4)
                tape_momentum_pct = round(float(tf.get("momentum_pct") or 0.0) * 100, 4)
        except BinanceClientError as exc:
            self.logger.warning("TradeMonitor: trade_flow no disponible para %s: %s", symbol, exc)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("TradeMonitor: trade_flow error para %s: %s", symbol, exc)

        # Calcular distancias SL/TP en %
        sl_distance_pct = round(
            abs(mark_price - stop_loss) / entry_price * 100, 4
        ) if entry_price > 0 and stop_loss > 0 else None
        tp_distance_pct = round(
            abs(take_profit - mark_price) / entry_price * 100, 4
        ) if entry_price > 0 and take_profit > 0 else None

        return {
            "symbol": symbol,
            "side": position.get("side"),
            "scenario": position.get("scenario"),
            "entry_price": round(entry_price, 6),
            "mark_price": round(mark_price, 6),
            "stop_loss": round(stop_loss, 6),
            "take_profit": round(take_profit, 6),
            "trailing_tier": trailing_tier,
            "unrealized_pnl_usdt": unrealized_usdt,
            "unrealized_pnl_pct": unrealized_pct,
            "mfe_pct": mfe_pct,
            "mae_pct": mae_pct,
            "hold_minutes": hold_minutes,
            "rsi": rsi,
            "volume_ratio": volume_ratio,
            "atr_pct": atr_pct,
            "orderbook_imbalance": orderbook_imbalance,
            "trade_flow_score": trade_flow_score,
            "tape_momentum_pct": tape_momentum_pct,
            "sl_distance_pct": sl_distance_pct,
            "tp_distance_pct": tp_distance_pct,
            "entry_rsi": position.get("entry_rsi"),
            "ai_confidence_at_entry": position.get("ai_confidence"),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # PROMPT BUILDER
    # ─────────────────────────────────────────────────────────────────────────

    def _build_monitor_prompt(
        self,
        state: dict[str, Any],
        recent_verdicts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Construye el payload JSON que se envia a la IA en cada ciclo."""

        # Resumen de veredictos previos — la "memoria" del monitor
        memory_context: list[dict[str, Any]] = []
        for v in recent_verdicts:
            elapsed = "--"
            if v.get("timestamp"):
                try:
                    ts = datetime.fromisoformat(str(v["timestamp"]))
                    secs = (datetime.now(timezone.utc) - ts).total_seconds()
                    elapsed = f"hace {int(secs // 60)}min {int(secs % 60)}s"
                except ValueError:
                    pass
            prev_state = v.get("state") or {}
            memory_context.append({
                "elapsed": elapsed,
                "action": v.get("action"),
                "rationale": str(v.get("rationale", ""))[:100],
                "pnl_pct_then": prev_state.get("unrealized_pnl_pct"),
                "flow_score_then": prev_state.get("trade_flow_score"),
                "volume_ratio_then": prev_state.get("volume_ratio"),
                "orderbook_then": prev_state.get("orderbook_imbalance"),
            })

        # Prompt estructurado para la IA
        return {
            "task": "dynamic_trade_monitoring",
            "symbol": state["symbol"],
            "binance_round_trip_fee_pct": 0.20,
            "breakeven_sl_pct": 0.25,
            "current_state": state,
            "recent_ai_verdicts_memory": memory_context,
            "decision_rules": {
                "HOLD": [
                    "trade_flow_score > 0.52 Y volume_ratio > 0.8 Y unrealized_pnl_pct > -0.3",
                    "El momentum sigue activo; dejar correr al SL/TP/trailing configurados.",
                ],
                "UPDATE_SL": [
                    "unrealized_pnl_pct >= 0.25 Y (volume_ratio < 0.65 O trade_flow_score < 0.42) Y trailing_tier == 0",
                    "El trade tiene ganancia pero el momentum se esta secando.",
                    "Mover SL a entry + 0.25% para asegurar comisiones (0.2% round-trip) + micro-profit.",
                    "Si ya hay trailing_tier > 0 el sistema lo maneja; no duplicar con UPDATE_SL.",
                ],
                "EMERGENCY_CLOSE": [
                    "SOLO si hold_minutes >= 5 Y tape_momentum_pct < -0.5 Y orderbook_imbalance < 0.35 Y unrealized_pnl_pct < -0.50",
                    "O: hold_minutes >= 5 Y unrealized_pnl_pct < -0.80 Y volume_ratio < 0.30 (capitulacion de volumen severa)",
                    "O: hold_minutes > 60 Y unrealized_pnl_pct < -0.10 Y mfe_pct < 0.10 (muerte lenta sin traccion tras 1 hora)",
                    "NUNCA cerrar si hold_minutes < 5. El mercado necesita tiempo para reaccionar tras la entrada.",
                    "Cierre inmediato a mercado. Usar solo en situaciones de riesgo real, no por volatilidad normal.",
                ],
            },
            "priority": "PRIMERO NO PERDER. Pero dar tiempo al mercado: no cerrar en los primeros 5 minutos salvo catastrofe.",
            "instructions": [
                "Responde SOLO JSON valido con: action, rationale, new_sl_pct (incluir solo si action=UPDATE_SL).",
                "action debe ser exactamente HOLD, UPDATE_SL o EMERGENCY_CLOSE.",
                "new_sl_pct: decimal sobre entry_price (0.0025 = +0.25%). Solo incluir si action=UPDATE_SL.",
                "rationale: una frase concisa en espanol (max 150 chars) explicando la decision.",
                "Usa los veredictos previos en 'recent_ai_verdicts_memory' para contexto. Si empeoro, escala.",
                "Un PnL de -0.03% a -0.20% en los primeros minutos es normal (spread + slippage). Responde HOLD.",
                "No inventes acciones. No devuelvas markdown. Solo el JSON.",
            ],
        }

    # ─────────────────────────────────────────────────────────────────────────
    # LLAMADA A LA IA
    # ─────────────────────────────────────────────────────────────────────────

    def _query_ai(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Consulta OpenRouter con fallback y parsea la respuesta JSON."""
        if not self.settings.openrouter_api_key:
            self.logger.warning("TradeMonitor: OpenRouter no configurado. Accion por defecto: HOLD.")
            return {
                "action": "HOLD",
                "rationale": "OpenRouter no configurado.",
                "model": "none",
                "consulted": False,
            }

        models_to_try: list[str] = [self.settings.openrouter_model]
        for m in self.settings.openrouter_fallback_models:
            if m and m not in models_to_try:
                models_to_try.append(m)

        system_prompt = (
            "Eres el cerebro de un bot de trading spot en Binance. "
            "Tu funcion exclusiva es monitorear posiciones abiertas y decidir cada minuto: "
            "HOLD (mantener), UPDATE_SL (mover stop a breakeven+), o EMERGENCY_CLOSE (cortar ya). "
            "Prioridad absoluta: primero no perder. Las comisiones de Binance son 0.1% por lado "
            "(0.2% round-trip); el profit debe superar eso para que valga la pena mantener. "
            "Responde solo con JSON valido, sin markdown, sin texto adicional fuera del JSON."
        )

        last_error = "no_models"
        for model_name in models_to_try:
            try:
                response = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.settings.openrouter_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model_name,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {
                                "role": "user",
                                "content": json.dumps(payload, ensure_ascii=False),
                            },
                        ],
                        "temperature": 0.05,
                        "response_format": {"type": "json_object"},
                    },
                    timeout=6.0,
                )
                response.raise_for_status()
                raw = response.json()["choices"][0]["message"]["content"]
                parsed: dict[str, Any] = json.loads(raw)

                # Normalizar action
                action = str(parsed.get("action", "HOLD")).upper().strip()
                if action not in {"HOLD", "UPDATE_SL", "EMERGENCY_CLOSE"}:
                    self.logger.warning(
                        "TradeMonitor: accion desconocida '%s'; forzando HOLD.", action
                    )
                    action = "HOLD"
                parsed["action"] = action
                parsed.setdefault("rationale", "Sin explicacion.")
                parsed["model"] = model_name
                parsed["consulted"] = True

                self.logger.info(
                    "TradeMonitor AI | modelo=%s symbol=%s action=%s rationale=%s",
                    model_name,
                    payload.get("symbol"),
                    action,
                    str(parsed.get("rationale", ""))[:120],
                )
                return parsed

            except (requests.Timeout, TimeoutError):
                last_error = "timeout"
                self.logger.warning(
                    "TradeMonitor: timeout con modelo=%s. Probando fallback.", model_name
                )
            except (
                requests.RequestException,
                KeyError,
                ValueError,
                TypeError,
                json.JSONDecodeError,
            ) as exc:
                last_error = str(exc)
                self.logger.warning(
                    "TradeMonitor: error con modelo=%s (%s). Probando fallback.",
                    model_name,
                    exc,
                )

        self.logger.error(
            "TradeMonitor: OpenRouter sin respuesta tras todos los modelos. last_error=%s. HOLD por seguridad.",
            last_error,
        )
        return {
            "action": "HOLD",
            "rationale": f"OpenRouter sin respuesta ({last_error}); manteniendo posicion.",
            "model": "fallback_hold",
            "consulted": True,
            "error": last_error,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # PUNTO DE ENTRADA PRINCIPAL
    # ─────────────────────────────────────────────────────────────────────────

    def evaluate(
        self,
        position: dict[str, Any],
        scan_result: dict[str, Any] | None,
        client: BinanceDataClient,
    ) -> dict[str, Any]:
        """Evalua la posicion abierta y devuelve el veredicto de la IA.

        Returns:
            dict con: action, rationale, model, state, new_sl_price, timestamp, symbol.
        """
        symbol = str(position.get("symbol") or "")
        self.logger.info("TradeMonitor: evaluando posicion de %s", symbol)

        # 1) Recopilar estado
        try:
            state = self._collect_position_state(position, scan_result, client)
        except Exception as exc:  # noqa: BLE001
            self.logger.error(
                "TradeMonitor: fallo al recopilar estado para %s: %s", symbol, exc
            )
            return {
                "action": "HOLD",
                "rationale": f"Error recopilando datos: {exc}",
                "model": "error_hold",
                "consulted": False,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
            }

        # 2) Guard de tiempo minimo: nunca EMERGENCY_CLOSE ni UPDATE_SL antes de 5 minutos.
        # El mercado necesita tiempo para reaccionar al fill; los primeros minutos son ruido.
        hold_minutes_now = float(state.get("hold_minutes") or 0.0)
        _MIN_HOLD_MINUTES = 5.0
        if hold_minutes_now < _MIN_HOLD_MINUTES:
            early_verdict: dict[str, Any] = {
                "action": "HOLD",
                "rationale": f"Hold minimo ({_MIN_HOLD_MINUTES:.0f}min) no cumplido ({hold_minutes_now:.1f}min). Esperando.",
                "model": "hold_guard",
                "consulted": False,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "state": state,
                "new_sl_price": None,
                "new_sl_pct": None,
                "memory_window": 0,
            }
            self._persist_verdict(early_verdict)
            self.logger.info(
                "TradeMonitor HOLD-GUARD: %s hold=%.1fmin < %.0fmin minimo.",
                symbol, hold_minutes_now, _MIN_HOLD_MINUTES,
            )
            return early_verdict

        # 3) Memoria: cargar veredictos previos
        recent_verdicts = self._load_recent_verdicts(symbol, n=_MONITOR_MEMORY_WINDOW)

        # 4) Construir prompt
        prompt = self._build_monitor_prompt(state, recent_verdicts)

        # 5) Consultar IA
        ai_response = self._query_ai(prompt)

        # 5b) Segunda guardia: si la IA propone EMERGENCY_CLOSE con PnL > -0.40%,
        # degradar a HOLD. La IA a veces es demasiado conservadora en volatilidad normal.
        if ai_response.get("action") == "EMERGENCY_CLOSE":
            pnl_pct = float(state.get("unrealized_pnl_pct") or 0.0)
            if pnl_pct > -0.40:
                self.logger.warning(
                    "TradeMonitor: EMERGENCY_CLOSE degradado a HOLD. PnL=%.3f%% > -0.40%% (no es emergencia real).",
                    pnl_pct,
                )
                ai_response["action"] = "HOLD"
                ai_response["rationale"] = (
                    f"[Override] PnL={pnl_pct:.3f}%% no justifica emergencia; manteniendo posicion."
                )

        # 5) Calcular nuevo precio de SL si UPDATE_SL
        new_sl_pct = float(ai_response.get("new_sl_pct") or 0.0025)  # default +0.25%
        entry_price = float(position.get("entry_price") or 0.0)
        new_sl_price: float | None = None
        if ai_response.get("action") == "UPDATE_SL" and entry_price > 0:
            new_sl_price = round(entry_price * (1.0 + new_sl_pct), 8)

        # 6) Construir veredicto final
        verdict: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "action": ai_response.get("action", "HOLD"),
            "rationale": ai_response.get("rationale", ""),
            "model": ai_response.get("model", self.settings.openrouter_model),
            "consulted": bool(ai_response.get("consulted", True)),
            "state": state,
            "new_sl_price": new_sl_price,
            "new_sl_pct": new_sl_pct if ai_response.get("action") == "UPDATE_SL" else None,
            "memory_window": len(recent_verdicts),
        }

        # 7) Persistir para el dashboard
        self._persist_verdict(verdict)

        self.logger.info(
            "TradeMonitor veredicto: symbol=%s action=%s pnl_pct=%.3f%% flow=%.3f tier=%s",
            symbol,
            verdict["action"],
            float(state.get("unrealized_pnl_pct") or 0.0),
            float(state.get("trade_flow_score") or 0.5),
            int(state.get("trailing_tier") or 0),
        )
        return verdict
