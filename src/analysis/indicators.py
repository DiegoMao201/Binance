from __future__ import annotations

import pandas as pd

from src.utils.config import Settings


def compute_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()

    data["ema_fast"] = data["close"].ewm(span=9, adjust=False).mean()
    data["ema_slow"] = data["close"].ewm(span=20, adjust=False).mean()
    # Medias largas para deteccion de regimen (trend / range / chop).
    # No se usan como gate clasico; alimentan el clasificador de regimen.
    data["ema_50"] = data["close"].ewm(span=50, adjust=False).mean()
    data["ema_200"] = data["close"].ewm(span=200, adjust=False).mean()

    delta = data["close"].diff()
    gains = delta.clip(lower=0).rolling(window=14).mean()
    losses = (-delta.clip(upper=0)).rolling(window=14).mean()
    rs = gains / losses.replace(0, pd.NA)
    data["rsi"] = 100 - (100 / (1 + rs))
    data["rsi"] = data["rsi"].fillna(50)

    rolling_mean = data["close"].rolling(window=20).mean()
    rolling_std = data["close"].rolling(window=20).std()
    data["bb_mid"] = rolling_mean
    data["bb_upper"] = rolling_mean + (rolling_std * 2)
    data["bb_lower"] = rolling_mean - (rolling_std * 2)
    data["bb_width_pct"] = ((data["bb_upper"] - data["bb_lower"]) / data["close"]).fillna(0)

    high_low = data["high"] - data["low"]
    high_close = (data["high"] - data["close"].shift()).abs()
    low_close = (data["low"] - data["close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    data["atr"] = true_range.rolling(window=14).mean()
    data["atr_pct"] = (data["atr"] / data["close"]).fillna(0)
    data["volume_mean_20"] = data["volume"].rolling(window=20).mean()
    data["volume_ratio"] = (data["volume"] / data["volume_mean_20"]).fillna(0)
    data["ema_slow_slope"] = data["ema_slow"].diff().fillna(0)
    # Slope porcentual de EMA50 sobre 5 velas: input crudo para regimen.
    data["ema_50_slope_pct"] = (data["ema_50"].diff(5) / data["close"]).fillna(0)

    return data.dropna().reset_index(drop=True)


def _classify_regime(latest: pd.Series, bb_width_pct: float) -> str:
    """Clasifica el regimen del mercado.

    - trending_up:   precio > EMA200, EMA50 sobre EMA200, slope EMA50 > 0
    - trending_down: precio < EMA200 y EMA50 < EMA200
    - range:         precio cerca de EMA200 (+/-2%) y BB width > 1.5%
    - chop:          baja volatilidad (BB width < 0.5%), evitar entradas
    """
    close = float(latest["close"])
    ema_50 = float(latest.get("ema_50", close))
    ema_200 = float(latest.get("ema_200", close))
    slope_pct = float(latest.get("ema_50_slope_pct", 0.0))

    if bb_width_pct < 0.005:
        return "chop"

    above_long_ma = close > ema_200 and ema_50 > ema_200
    below_long_ma = close < ema_200 and ema_50 < ema_200
    distance_pct = abs(close - ema_200) / ema_200 if ema_200 > 0 else 0.0

    if above_long_ma and slope_pct > 0.0005:
        return "trending_up"
    if below_long_ma and slope_pct < -0.0005:
        return "trending_down"
    if distance_pct < 0.02:
        return "range"
    return "range"


def _compute_setup_score(
    *,
    rsi: float,
    rsi_slope: float,
    volume_ratio: float,
    volume_acceleration: float,
    bb_position_pct: float,
    candle_body_pct: float,
    consecutive_green: int,
    ema_slow_slope: float,
    scenario: str | None,
) -> float:
    """Score 0-1 de calidad del setup, combinando 6 factores ortogonales.

    Filosofia: cada factor aporta hasta ~16% del score. Un setup ideal cumple
    casi todo (rebote desde sobreventa con volumen creciente, vela fuerte
    verde y RSI subiendo).
    """
    if scenario not in {"A", "B", "C"}:
        return 0.0

    # 1. RSI sweet spot por escenario (16%)
    if scenario == "A":
        # Pullback ideal: RSI 30-45
        rsi_score = max(0.0, 1.0 - abs(rsi - 38) / 25)
    elif scenario == "B":
        # Sobreventa extrema: cuanto mas bajo, mejor (cap en 18)
        rsi_score = max(0.0, min(1.0, (32 - rsi) / 14))
    else:
        # Continuacion de tendencia: zona de momentum sano, sin sobre-extenderse.
        rsi_score = max(0.0, 1.0 - abs(rsi - 58) / 16)

    # 2. RSI slope subiendo = recuperacion confirmada (16%)
    rsi_slope_score = max(0.0, min(1.0, rsi_slope / 8.0))

    # 3. Volume ratio sobre el promedio (16%)
    volume_score = max(0.0, min(1.0, (volume_ratio - 1.0) / 1.5))

    # 4. Volume acceleration: ultima vela vs ultimas 3 (16%)
    accel_score = max(0.0, min(1.0, (volume_acceleration - 1.0) / 1.5))

    # 5. BB position (16%):
    # - A/B: reversion, mejor cerca de BB lower.
    # - C: continuacion, mejor en la mitad-superior sin estar totalmente extendido.
    if scenario in {"A", "B"}:
        bb_score = max(0.0, 1.0 - bb_position_pct) if bb_position_pct < 0.5 else 0.0
    else:
        bb_score = max(0.0, 1.0 - abs(bb_position_pct - 0.65) / 0.35)

    # 6. Vela fuerte verde + momentum (20%)
    body_score = candle_body_pct
    momentum_bonus = min(0.3, consecutive_green * 0.1) if consecutive_green > 0 else 0.0
    candle_score = min(1.0, body_score + momentum_bonus)

    # Penalty si la EMA20 va plana o cayendo (mercado sin sustento)
    slope_factor = 1.0 if ema_slow_slope >= 0 else 0.85

    raw = (
        rsi_score * 0.16
        + rsi_slope_score * 0.16
        + volume_score * 0.16
        + accel_score * 0.16
        + bb_score * 0.16
        + candle_score * 0.20
    )
    return round(min(1.0, max(0.0, raw * slope_factor)), 4)



def build_technical_signal(frame: pd.DataFrame, settings: Settings | None = None) -> dict[str, object]:
    latest = frame.iloc[-1]
    previous = frame.iloc[-2]

    # Lecturas crudas
    rsi = float(latest["rsi"])
    close = float(latest["close"])
    open_price = float(latest["open"])
    ema_slow = float(latest["ema_slow"])

    # Umbrales (con fallback si no se pasa settings)
    rsi_max_a = float(settings.scenario_a_rsi_max) if settings else 45.0
    rsi_max_b = float(settings.scenario_b_rsi_max) if settings else 32.0

    bullish_cross = bool(previous["ema_fast"] <= previous["ema_slow"] and latest["ema_fast"] > latest["ema_slow"])
    bearish_cross = bool(previous["ema_fast"] >= previous["ema_slow"] and latest["ema_fast"] < latest["ema_slow"])
    overbought = bool(rsi > 70)
    green_candle = bool(close > open_price)

    # --- Fuerza de vela: body / range (0=doji, 1=vela perfecta sin mecha) ---
    candle_range = float(latest["high"]) - float(latest["low"])
    candle_body = abs(close - open_price)
    candle_body_pct = round(candle_body / candle_range, 4) if candle_range > 0 else 0.0

    # --- Momentum: cuantas velas verdes consecutivas al cierre de la ultima ---
    consecutive_green = 0
    for i in range(len(frame) - 1, max(len(frame) - 10, -1), -1):
        row = frame.iloc[i]
        if float(row["close"]) > float(row["open"]):
            consecutive_green += 1
        else:
            break

    # --- Aceleracion de volumen: ultima vela vs promedio de las 3 anteriores ---
    last3_vol_mean = float(frame["volume"].iloc[-4:-1].mean()) if len(frame) >= 4 else float(frame["volume"].iloc[-1])
    volume_acceleration = round(float(latest["volume"]) / last3_vol_mean, 4) if last3_vol_mean > 0 else 1.0

    # --- Pendiente del RSI en las ultimas 3 velas (sube = recuperacion real) ---
    if len(frame) >= 3:
        rsi_slope = round(float(frame["rsi"].iloc[-1]) - float(frame["rsi"].iloc[-3]), 2)
    else:
        rsi_slope = 0.0

    # --- Posicion del precio dentro de las Bandas de Bollinger (0=BB lower, 1=BB upper) ---
    bb_upper = float(latest["bb_upper"])
    bb_lower = float(latest["bb_lower"])
    bb_range = bb_upper - bb_lower
    bb_position_pct = round((close - bb_lower) / bb_range, 4) if bb_range > 0 else 0.5

    # Logica OR
    scenario_a = bool(rsi <= rsi_max_a and close > ema_slow)            # Pullback con tendencia
    scenario_b = bool(rsi <= rsi_max_b and green_candle)                # Sobreventa extrema con freno
    # Continuacion de tendencia con momentum sano (evita depender solo de RSI bajo).
    ema_slow_slope = float(latest["ema_slow_slope"])

    scenario_c = bool(
        close > ema_slow
        and green_candle
        and ema_slow_slope > 0
        and 52 <= rsi <= 68
        and (volume_acceleration >= 1.05 or float(latest["volume_ratio"]) >= 0.90)
    )

    if scenario_a:
        signal = "buy"
        scenario = "A"
    elif scenario_b:
        signal = "buy"
        scenario = "B"
    elif scenario_c:
        signal = "buy"
        scenario = "C"
    elif bearish_cross or overbought:
        signal = "sell"
        scenario = None
    else:
        signal = "hold"
        scenario = None

    confidence = abs(latest["ema_fast"] - latest["ema_slow"]) / latest["close"]

    bb_width_pct_value = round(float(latest["bb_width_pct"]), 4)
    regime = _classify_regime(latest, bb_width_pct_value)
    setup_score = _compute_setup_score(
        rsi=rsi,
        rsi_slope=rsi_slope,
        volume_ratio=float(latest["volume_ratio"]),
        volume_acceleration=volume_acceleration,
        bb_position_pct=bb_position_pct,
        candle_body_pct=candle_body_pct,
        consecutive_green=consecutive_green,
        ema_slow_slope=float(latest["ema_slow_slope"]),
        scenario=scenario,
    )

    return {
        "signal": signal,
        "scenario": scenario,
        "scenario_a": scenario_a,
        "scenario_b": scenario_b,
        "scenario_c": scenario_c,
        "confidence": round(float(confidence), 4),
        "rsi": round(rsi, 2),
        "close": round(close, 4),
        "open": round(open_price, 4),
        "high": round(float(latest["high"]), 4),
        "low": round(float(latest["low"]), 4),
        "ema_slow": round(ema_slow, 4),
        "ema_50": round(float(latest.get("ema_50", ema_slow)), 4),
        "ema_200": round(float(latest.get("ema_200", ema_slow)), 4),
        "ema_50_slope_pct": round(float(latest.get("ema_50_slope_pct", 0.0)), 6),
        "regime": regime,
        "setup_score": setup_score,
        "bb_width_pct": bb_width_pct_value,
        "bb_position_pct": bb_position_pct,
        "atr_pct": round(float(latest["atr_pct"]), 4),
        "volume_ratio": round(float(latest["volume_ratio"]), 4),
        "volume_acceleration": volume_acceleration,
        "ema_slow_slope": round(float(latest["ema_slow_slope"]), 6),
        "candle_body_pct": candle_body_pct,
        "consecutive_green": consecutive_green,
        "rsi_slope": rsi_slope,
        "bullish_cross": bullish_cross,
        "bearish_cross": bearish_cross,
        "green_candle": green_candle,
        "overbought": overbought,
    }