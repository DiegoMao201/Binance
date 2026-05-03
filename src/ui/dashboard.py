from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.utils.config import load_settings


EMERALD = "#00C781"
CRIMSON = "#D62839"
BG = "#0A0F14"
PANEL = "#111923"
TEXT = "#E5EDF5"
MUTED = "#8EA0B5"


st.set_page_config(page_title="OptiFerre-Trader", layout="wide")
settings = load_settings()

st.markdown(
    f"""
    <style>
    .stApp {{ background-color: {BG}; color: {TEXT}; }}
    [data-testid="stSidebar"] {{ background: linear-gradient(180deg, #0d141c 0%, #101822 100%); }}
    [data-testid="metric-container"] {{ background-color: {PANEL}; border: 1px solid #1D2A38; padding: 14px; border-radius: 8px; }}
    .block-container {{ padding-top: 1.2rem; padding-bottom: 1.2rem; }}
    h1, h2, h3, p, div, span, label {{ color: {TEXT}; }}
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(ttl=10)
def load_bot_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text(encoding="utf-8"))


@st.cache_data(ttl=10)
def load_json(path: Path, fallback):
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def compute_bot_online(status: dict) -> bool:
    heartbeat_at = status.get("heartbeat_at")
    if not heartbeat_at:
        return False
    last_seen = datetime.fromisoformat(heartbeat_at)
    delta = datetime.now(timezone.utc) - last_seen
    return delta.total_seconds() <= (settings.poll_interval_seconds * 2)


def build_marker_trace(signal_history: pd.DataFrame, signal_column: str, name: str, color: str, symbol: str):
    subset = signal_history[signal_history[signal_column].isin(["buy", "sell"])].copy()
    if subset.empty:
        return None

    subset["marker_symbol"] = subset[signal_column].map({"buy": symbol, "sell": f"{symbol}-down"})
    subset["marker_color"] = subset[signal_column].map({"buy": EMERALD, "sell": CRIMSON})
    return go.Scatter(
        x=subset["timestamp"],
        y=subset["technical_price"],
        mode="markers",
        name=name,
        marker={"size": 12, "symbol": subset["marker_symbol"], "color": subset["marker_color"], "line": {"width": 1, "color": color}},
        text=subset[signal_column],
        hovertemplate="%{x}<br>%{text}<br>Precio=%{y}<extra></extra>",
    )


state = load_bot_state(settings.state_file)
status = load_json(settings.status_file, {})
order_history = load_json(settings.order_history_file, [])
signal_history_data = load_json(settings.signal_history_file, [])
is_online = compute_bot_online(status)

st.title("OptiFerre-Trader")
st.caption("Monitor Bloomberg-style para Binance, IA y ejecución prudente.")

st.sidebar.header("Control de Mandos")
st.sidebar.write(f"Par activo: {settings.trading_symbol}")
st.sidebar.write(f"Timeframe: {settings.timeframe}")
st.sidebar.write(f"Modo: {'DRY RUN' if settings.dry_run else 'LIVE'}")
st.sidebar.write(f"Heartbeat: {status.get('heartbeat_at', 'sin datos')}")
st.sidebar.write(f"Estado bot: {'ONLINE' if is_online else 'OFFLINE'}")
st.sidebar.write(f"Último detalle: {status.get('detail', 'n/d')}")
if st.sidebar.button("Refrescar panel"):
    st.cache_data.clear()
    st.rerun()

if not state:
    st.warning("Todavía no hay estado generado. Ejecuta primero main_loop.py.")
    st.stop()

market = pd.DataFrame(state.get("market", []))
if market.empty:
    st.warning("No hay datos de mercado disponibles en el estado actual.")
    st.stop()

market["timestamp"] = pd.to_datetime(market["timestamp"], utc=True)
signal_history = pd.DataFrame(signal_history_data)
if not signal_history.empty:
    signal_history["timestamp"] = pd.to_datetime(signal_history["timestamp"], utc=True)

orders = pd.DataFrame(order_history)

col1, col2, col3, col4 = st.columns(4)
col1.metric("Balance Actual (USDT)", f"{state.get('risk', {}).get('balance_usd', 0):.2f}")
col2.metric("PnL Diario", f"{state.get('risk', {}).get('daily_pnl_pct', 0) * 100:.2f}%")
col3.metric("Kill Switch", "ACTIVO" if state.get("risk", {}).get("kill_switch_triggered") else "SEGURO")
col4.metric("Bot", "ONLINE" if is_online else "OFFLINE")

figure = go.Figure()
figure.add_trace(
    go.Candlestick(
        x=market["timestamp"],
        open=market["open"],
        high=market["high"],
        low=market["low"],
        close=market["close"],
        name="Precio",
        increasing_line_color=EMERALD,
        decreasing_line_color=CRIMSON,
    )
)
figure.add_trace(go.Scatter(x=market["timestamp"], y=market["ema_fast"], name="EMA 9", line={"color": "#F7B801", "width": 1.5}))
figure.add_trace(go.Scatter(x=market["timestamp"], y=market["ema_slow"], name="EMA 20", line={"color": "#4CC9F0", "width": 1.5}))
figure.add_trace(go.Scatter(x=market["timestamp"], y=market["bb_upper"], name="BB Upper", line={"color": "#7D8597", "width": 1}, opacity=0.45))
figure.add_trace(go.Scatter(x=market["timestamp"], y=market["bb_lower"], name="BB Lower", line={"color": "#7D8597", "width": 1}, fill="tonexty", fillcolor="rgba(125,133,151,0.08)", opacity=0.45))

if not signal_history.empty:
    technical_markers = build_marker_trace(signal_history, "technical_signal", "Señal técnica", "#0B3D2E", "triangle-up")
    ai_markers = build_marker_trace(signal_history, "ai_signal", "Señal IA", "#4A1C22", "diamond")
    if technical_markers:
        figure.add_trace(technical_markers)
    if ai_markers:
        figure.add_trace(ai_markers)

figure.update_layout(
    height=680,
    xaxis_rangeslider_visible=False,
    paper_bgcolor=BG,
    plot_bgcolor=BG,
    font={"color": TEXT, "family": "Menlo, Consolas, monospace"},
    legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "left", "x": 0},
    margin={"l": 10, "r": 10, "t": 20, "b": 10},
)
figure.update_xaxes(gridcolor="#1C2733")
figure.update_yaxes(gridcolor="#1C2733")

st.plotly_chart(figure, width="stretch")

left, right = st.columns(2)
left.subheader("Decisión Actual")
left.json(state.get("decision", {}))

right.subheader("Opinión de IA")
right.json(state.get("ai_signal", {}))

st.subheader("Log de Operaciones")
if orders.empty:
    st.info("Todavía no hay órdenes ejecutadas o simuladas.")
else:
    columns = [
        column
        for column in ["timestamp", "symbol", "side", "status", "price", "amount", "notional_usdt", "stop_loss", "take_profit", "mode"]
        if column in orders.columns
    ]
    st.dataframe(orders[columns].sort_values("timestamp", ascending=False), width="stretch", hide_index=True)

st.subheader("Estado de Servicio")
status_left, status_right = st.columns(2)
status_left.write({"updated_at": state.get("updated_at", "n/d"), "heartbeat_at": status.get("heartbeat_at", "n/d")})
status_right.write({"kill_switch": state.get("risk", {}).get("kill_switch_triggered", False), "ultimo_detalle": status.get("detail", "n/d")})