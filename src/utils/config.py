from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")


@dataclass(slots=True)
class Settings:
    binance_api_key: str
    binance_api_secret: str
    binance_proxy_url: str
    binance_whitelist_ips: tuple[str, ...]
    openrouter_api_key: str
    openrouter_model: str
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str
    trading_symbol: str
    target_symbols: tuple[str, ...]
    max_global_open_positions: int
    symbol_scan_pause_seconds: float
    timeframe: str
    dry_run: bool
    initial_capital_usd: float
    max_risk_per_trade: float
    kill_switch_drawdown: float
    stop_loss_pct: float
    take_profit_pct: float
    max_position_hold_minutes: int
    time_profit_take_pct: float
    trailing_activation_pct: float
    trailing_sl_offset_pct: float
    time_stop_minutes: int
    time_stop_dead_zone_pct: float
    smart_hard_timeout_minutes: int
    smart_stagnation_minutes: int
    smart_stagnation_max_mfe_pct: float
    smart_stagnation_loss_cut_pct: float
    poll_interval_seconds: int
    log_level: str
    streamlit_port: int
    minimum_trade_usdt: float
    ai_confidence_threshold: float
    technical_confidence_threshold: float
    min_bb_width_pct: float
    min_atr_pct: float
    max_atr_pct: float
    min_volume_ratio: float
    trade_cooldown_minutes: int
    ai_min_interval_seconds: int
    position_size_pct: float
    scenario_a_rsi_max: float
    scenario_b_rsi_max: float
    logs_dir: Path
    state_file: Path
    log_file: Path
    status_file: Path
    control_file: Path
    order_history_file: Path
    signal_history_file: Path
    scan_history_file: Path
    open_positions_file: Path
    closed_trades_file: Path
    equity_history_file: Path


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    return value in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    logs_dir = Path(os.getenv("LOGS_DIR", str(BASE_DIR / "logs"))).expanduser()
    primary_symbol = os.getenv("TRADING_SYMBOL", "BTC/USDT")
    raw_targets = os.getenv("TARGET_SYMBOLS", primary_symbol)
    raw_whitelist_ips = os.getenv("BINANCE_WHITELIST_IPS", "")
    target_symbols = tuple(
        dict.fromkeys(  # preserva orden y deduplica
            sym.strip().upper() for sym in raw_targets.split(",") if sym.strip()
        )
    ) or (primary_symbol,)
    whitelist_ips = tuple(
        dict.fromkeys(ip.strip() for ip in raw_whitelist_ips.split(",") if ip.strip())
    )
    return Settings(
        binance_api_key=os.getenv("BINANCE_API_KEY", ""),
        binance_api_secret=os.getenv("BINANCE_API_SECRET", ""),
        binance_proxy_url=os.getenv("BINANCE_PROXY_URL", "").strip(),
        binance_whitelist_ips=whitelist_ips,
        telegram_enabled=_get_bool("TELEGRAM_ENABLED", False),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
        openrouter_model=os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1"),
        trading_symbol=primary_symbol,
        target_symbols=target_symbols,
        max_global_open_positions=int(os.getenv("MAX_GLOBAL_OPEN_POSITIONS", "1")),
        symbol_scan_pause_seconds=float(os.getenv("SYMBOL_SCAN_PAUSE_SECONDS", "1.0")),
        timeframe=os.getenv("TIMEFRAME", "1m"),
        dry_run=_get_bool("DRY_RUN", True),
        initial_capital_usd=float(os.getenv("INITIAL_CAPITAL_USD", "20")),
        max_risk_per_trade=float(os.getenv("MAX_RISK_PER_TRADE", "0.10")),
        kill_switch_drawdown=float(os.getenv("KILL_SWITCH_DRAWDOWN", "0.05")),
        stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "0.02")),
        take_profit_pct=float(os.getenv("TAKE_PROFIT_PCT", "0.03")),
        max_position_hold_minutes=int(os.getenv("MAX_POSITION_HOLD_MINUTES", "0")),
        time_profit_take_pct=float(os.getenv("TIME_PROFIT_TAKE_PCT", "0.0")),
        trailing_activation_pct=float(os.getenv("TRAILING_ACTIVATION_PCT", "0.008")),
        trailing_sl_offset_pct=float(os.getenv("TRAILING_SL_OFFSET_PCT", "0.002")),
        time_stop_minutes=int(os.getenv("TIME_STOP_MINUTES", "240")),
        time_stop_dead_zone_pct=float(os.getenv("TIME_STOP_DEAD_ZONE_PCT", "0.003")),
        smart_hard_timeout_minutes=int(os.getenv("SMART_HARD_TIMEOUT_MINUTES", "360")),
        smart_stagnation_minutes=int(os.getenv("SMART_STAGNATION_MINUTES", "120")),
        smart_stagnation_max_mfe_pct=float(os.getenv("SMART_STAGNATION_MAX_MFE_PCT", "0.004")),
        smart_stagnation_loss_cut_pct=float(os.getenv("SMART_STAGNATION_LOSS_CUT_PCT", "0.0045")),
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        streamlit_port=int(os.getenv("STREAMLIT_PORT", "8501")),
        minimum_trade_usdt=float(os.getenv("MINIMUM_TRADE_USDT", "10.1")),
        ai_confidence_threshold=float(os.getenv("AI_CONFIDENCE_THRESHOLD", "0.88")),
        technical_confidence_threshold=float(os.getenv("TECHNICAL_CONFIDENCE_THRESHOLD", "0.0008")),
        min_bb_width_pct=float(os.getenv("MIN_BB_WIDTH_PCT", "0.004")),
        min_atr_pct=float(os.getenv("MIN_ATR_PCT", "0.0015")),
        max_atr_pct=float(os.getenv("MAX_ATR_PCT", "0.018")),
        min_volume_ratio=float(os.getenv("MIN_VOLUME_RATIO", "1.10")),
        trade_cooldown_minutes=int(os.getenv("TRADE_COOLDOWN_MINUTES", "10")),
        ai_min_interval_seconds=int(os.getenv("AI_MIN_INTERVAL_SECONDS", "300")),
        position_size_pct=float(os.getenv("POSITION_SIZE_PCT", "0.95")),
        scenario_a_rsi_max=float(os.getenv("SCENARIO_A_RSI_MAX", "45")),
        scenario_b_rsi_max=float(os.getenv("SCENARIO_B_RSI_MAX", "32")),
        logs_dir=logs_dir,
        state_file=logs_dir / "bot_state.json",
        log_file=logs_dir / "bot.log",
        status_file=logs_dir / "status.json",
        control_file=logs_dir / "control.json",
        order_history_file=logs_dir / "order_history.json",
        signal_history_file=logs_dir / "signal_history.json",
        scan_history_file=logs_dir / "scan_history.json",
        open_positions_file=logs_dir / "open_positions.json",
        closed_trades_file=logs_dir / "closed_trades.json",
        equity_history_file=logs_dir / "equity_history.json",
    )