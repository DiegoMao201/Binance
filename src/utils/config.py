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
    openrouter_fallback_models: tuple[str, ...]
    openrouter_entry_model: str
    openrouter_entry_fallback_models: tuple[str, ...]
    openai_api_key: str  # OpenAI direct (gpt-4o-mini), bypasses OpenRouter
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
    smart_degradation_start_minutes: int
    smart_degradation_step_minutes: int
    smart_degradation_mfe_cap_step_pct: float
    smart_degradation_max_mfe_cap_pct: float
    smart_degradation_loss_cut_step_pct: float
    smart_degradation_min_loss_cut_pct: float
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
    max_spread_pct: float
    min_orderbook_imbalance: float
    min_trade_flow_score: float
    guardrail_relaxation: float
    trade_cooldown_minutes: int
    ai_min_interval_seconds: int
    position_size_pct: float
    scenario_a_rsi_max: float
    scenario_b_rsi_max: float
    ai_monitor_enabled: bool
    # ---- Micro-Structure Bailout ----
    bailout_enabled: bool
    bailout_min_drawdown_pct: float
    bailout_max_ob_imbalance: float
    bailout_max_flow_score: float
    bailout_min_hold_minutes: int
    # ---- PAMM Webhook ----
    # URL of the Next.js PAMM allocation endpoint.
    # e.g. https://tradingdiegomao.datovatenexuspro.com/api/webhooks/trade-closed
    pamm_webhook_url: str
    # Shared secret — must match WEBHOOK_SECRET in Coolify (Next.js env).
    webhook_secret: str
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
    raw_fallback_models = os.getenv(
        "OPENROUTER_FALLBACK_MODELS",
        "openai/gpt-oss-120b:free,openrouter/free,nvidia/nemotron-3-super-120b-a12b:free",
    )
    raw_entry_fallback_models = os.getenv(
        "OPENROUTER_ENTRY_FALLBACK_MODELS",
        "openai/gpt-4.1-mini,nvidia/nemotron-3-super-120b-a12b:free",
    )
    target_symbols = tuple(
        dict.fromkeys(  # preserva orden y deduplica
            sym.strip().upper() for sym in raw_targets.split(",") if sym.strip()
        )
    ) or (primary_symbol,)
    whitelist_ips = tuple(
        dict.fromkeys(ip.strip() for ip in raw_whitelist_ips.split(",") if ip.strip())
    )
    fallback_models = tuple(
        dict.fromkeys(model.strip() for model in raw_fallback_models.split(",") if model.strip())
    )
    entry_fallback_models = tuple(
        dict.fromkeys(model.strip() for model in raw_entry_fallback_models.split(",") if model.strip())
    )
    return Settings(
        binance_api_key=os.getenv("BINANCE_API_KEY", ""),
        binance_api_secret=os.getenv("BINANCE_API_SECRET", ""),
        binance_proxy_url=os.getenv("BINANCE_PROXY_URL", "").strip(),
        binance_whitelist_ips=whitelist_ips,
        telegram_enabled=_get_bool("TELEGRAM_ENABLED", False),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
        openrouter_model=os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openrouter_fallback_models=fallback_models,
        openrouter_entry_model=os.getenv("OPENROUTER_ENTRY_MODEL", "google/gemini-2.5-flash-preview-05-14"),
        openrouter_entry_fallback_models=entry_fallback_models,
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
        smart_hard_timeout_minutes=int(os.getenv("SMART_HARD_TIMEOUT_MINUTES", "180")),
        smart_stagnation_minutes=int(os.getenv("SMART_STAGNATION_MINUTES", "75")),
        smart_stagnation_max_mfe_pct=float(os.getenv("SMART_STAGNATION_MAX_MFE_PCT", "0.0035")),
        smart_stagnation_loss_cut_pct=float(os.getenv("SMART_STAGNATION_LOSS_CUT_PCT", "0.0035")),
        smart_degradation_start_minutes=int(os.getenv("SMART_DEGRADATION_START_MINUTES", "45")),
        smart_degradation_step_minutes=int(os.getenv("SMART_DEGRADATION_STEP_MINUTES", "30")),
        smart_degradation_mfe_cap_step_pct=float(os.getenv("SMART_DEGRADATION_MFE_CAP_STEP_PCT", "0.0004")),
        smart_degradation_max_mfe_cap_pct=float(os.getenv("SMART_DEGRADATION_MAX_MFE_CAP_PCT", "0.008")),
        smart_degradation_loss_cut_step_pct=float(os.getenv("SMART_DEGRADATION_LOSS_CUT_STEP_PCT", "0.00035")),
        smart_degradation_min_loss_cut_pct=float(os.getenv("SMART_DEGRADATION_MIN_LOSS_CUT_PCT", "0.0018")),
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        streamlit_port=int(os.getenv("STREAMLIT_PORT", "8501")),
        minimum_trade_usdt=float(os.getenv("MINIMUM_TRADE_USDT", "10.1")),
        ai_confidence_threshold=float(os.getenv("AI_CONFIDENCE_THRESHOLD", "0.55")),  # Coolify: 0.55 (era 0.62)
        technical_confidence_threshold=float(os.getenv("TECHNICAL_CONFIDENCE_THRESHOLD", "0.0")),  # Coolify: 0.0 (era 0.0008)
        min_bb_width_pct=float(os.getenv("MIN_BB_WIDTH_PCT", "0.0")),  # Coolify: 0.0 (era 0.004)
        min_atr_pct=float(os.getenv("MIN_ATR_PCT", "0.001")),  # Coolify: 0.001 (era 0.0015)
        max_atr_pct=float(os.getenv("MAX_ATR_PCT", "0.022")),  # Coolify: 0.022 (era 0.018)
        min_volume_ratio=float(os.getenv("MIN_VOLUME_RATIO", "0.15")),  # Coolify: 0.15 (era 1.10 → bloqueaba casi todo)
        max_spread_pct=float(os.getenv("MAX_SPREAD_PCT", "0.0015")),
        min_orderbook_imbalance=float(os.getenv("MIN_ORDERBOOK_IMBALANCE", "0.45")),
        min_trade_flow_score=float(os.getenv("MIN_TRADE_FLOW_SCORE", "0.45")),
        guardrail_relaxation=float(os.getenv("GUARDRAIL_RELAXATION", "0.08")),
        trade_cooldown_minutes=int(os.getenv("TRADE_COOLDOWN_MINUTES", "5")),  # Coolify: 5 (era 10)
        ai_min_interval_seconds=int(os.getenv("AI_MIN_INTERVAL_SECONDS", "120")),  # Coolify: 120 (era 300)
        position_size_pct=float(os.getenv("POSITION_SIZE_PCT", "0.95")),
        scenario_a_rsi_max=float(os.getenv("SCENARIO_A_RSI_MAX", "52")),  # Coolify: 52 (era 45)
        scenario_b_rsi_max=float(os.getenv("SCENARIO_B_RSI_MAX", "36")),  # Coolify: 36 (era 32)
        ai_monitor_enabled=_get_bool("AI_MONITOR_ENABLED", True),
        bailout_enabled=_get_bool("BAILOUT_ENABLED", True),
        bailout_min_drawdown_pct=float(os.getenv("BAILOUT_MIN_DRAWDOWN_PCT", "0.003")),
        bailout_max_ob_imbalance=float(os.getenv("BAILOUT_MAX_OB_IMBALANCE", "0.20")),
        bailout_max_flow_score=float(os.getenv("BAILOUT_MAX_FLOW_SCORE", "0.30")),
        bailout_min_hold_minutes=int(os.getenv("BAILOUT_MIN_HOLD_MINUTES", "10")),
        pamm_webhook_url=os.getenv("PAMM_WEBHOOK_URL", "").strip(),
        webhook_secret=os.getenv("WEBHOOK_SECRET", "").strip(),
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