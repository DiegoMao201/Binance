from __future__ import annotations

import pandas as pd

from src.utils.config import Settings


def compute_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()

    data["ema_fast"] = data["close"].ewm(span=9, adjust=False).mean()
    data["ema_slow"] = data["close"].ewm(span=20, adjust=False).mean()

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

    return data.dropna().reset_index(drop=True)


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

    if scenario_a:
        signal = "buy"
        scenario = "A"
    elif scenario_b:
        signal = "buy"
        scenario = "B"
    elif bearish_cross or overbought:
        signal = "sell"
        scenario = None
    else:
        signal = "hold"
        scenario = None

    confidence = abs(latest["ema_fast"] - latest["ema_slow"]) / latest["close"]
    return {
        "signal": signal,
        "scenario": scenario,
        "scenario_a": scenario_a,
        "scenario_b": scenario_b,
        "confidence": round(float(confidence), 4),
        "rsi": round(rsi, 2),
        "close": round(close, 4),
        "open": round(open_price, 4),
        "high": round(float(latest["high"]), 4),
        "low": round(float(latest["low"]), 4),
        "ema_slow": round(ema_slow, 4),
        "bb_width_pct": round(float(latest["bb_width_pct"]), 4),
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