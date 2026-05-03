from __future__ import annotations

import pandas as pd


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


def build_technical_signal(frame: pd.DataFrame) -> dict[str, str | float]:
    latest = frame.iloc[-1]
    previous = frame.iloc[-2]

    bullish_cross = bool(previous["ema_fast"] <= previous["ema_slow"] and latest["ema_fast"] > latest["ema_slow"])
    bearish_cross = bool(previous["ema_fast"] >= previous["ema_slow"] and latest["ema_fast"] < latest["ema_slow"])

    oversold = bool(latest["rsi"] < 30)
    overbought = bool(latest["rsi"] > 70)

    if bullish_cross or oversold:
        signal = "buy"
    elif bearish_cross or overbought:
        signal = "sell"
    else:
        signal = "hold"

    confidence = abs(latest["ema_fast"] - latest["ema_slow"]) / latest["close"]
    return {
        "signal": signal,
        "confidence": round(float(confidence), 4),
        "rsi": round(float(latest["rsi"]), 2),
        "close": round(float(latest["close"]), 4),
        "bb_width_pct": round(float(latest["bb_width_pct"]), 4),
        "atr_pct": round(float(latest["atr_pct"]), 4),
        "volume_ratio": round(float(latest["volume_ratio"]), 4),
        "ema_slow_slope": round(float(latest["ema_slow_slope"]), 6),
        "bullish_cross": bullish_cross,
        "bearish_cross": bearish_cross,
        "oversold": oversold,
        "overbought": overbought,
    }