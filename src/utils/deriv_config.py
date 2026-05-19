"""
src/utils/deriv_config.py
─────────────────────────────────────────────────────────────────────────────
Standalone settings dataclass for the Deriv async daemon.

Architectural decision: kept SEPARATE from `src.utils.config.Settings` so that
adding/removing Deriv knobs cannot ever break the running Binance Spot bot
(which depends on `Settings(slots=True)` field ordering).

All values are loaded from environment variables (read at process start via
`python-dotenv`). The Binance-side settings module already calls
`load_dotenv(BASE_DIR/.env)`, so the variables are available as long as
`src.utils.config` is imported once anywhere in the process.

REQUIRED env vars (see .env.deriv.example):
  DERIV_API_TOKEN   — long-lived API token from app.deriv.com → Settings → API
  DERIV_APP_ID      — public app id (default 1089 = "ws_app/sample")
  DERIV_ACCOUNT_ID  — optional explicit loginid; otherwise the token's default
                      account is used.
  DERIV_USER_ID     — UUID of the User row in PostgreSQL whose pool absorbs
                      Deriv PnL. Required for PAMM webhook attribution.

OPTIONAL safety knobs (all have defensive defaults):
  DERIV_SYMBOLS                  comma-separated, default "R_100,R_75,R_50"
  DERIV_DRY_RUN                  default true (fail-closed)
  DERIV_BANKROLL_USDT            default 50.0 — capital ring-fenced for Deriv
  DERIV_RISK_PER_TRADE_PCT       default 0.01 (1% of bankroll)
  DERIV_MAX_OPEN_CONTRACTS       default 3 (covers BOOM500 + CRASH500 + 1 extra)
  DERIV_MIN_SCORE                default 7.5 (out of 10)
  DERIV_MAX_DAILY_DD_PCT         default 0.02 (2 % daily DD lockout)
  DERIV_LOSS_STREAK_LOCKOUT      default 3 consecutive losses → 12 h lockout
  DERIV_LOCKOUT_HOURS            default 12
  DERIV_MAX_SPREAD_PCT           default 0.0010 (0.10 %)
  DERIV_CONTRACT_DURATION_SEC    default 300 (5 min)
  DERIV_MULTIPLIER               default 200 (minimum in common Deriv range: 80,200,400,600,800)
  DERIV_TAKE_PROFIT_PCT          default 0.012 (1.2 %)
  DERIV_STOP_LOSS_PCT            default 0.008 (0.8 %)
  DERIV_POLL_SECONDS             default 1.0 — how often the engine evaluates

BOOM/CRASH spike-gate knobs (Phase 12):
  DERIV_BOOM_CRASH_ESCAPE_VALVE       true/false (default false) — force escape path
                                       for BOOM/CRASH when no FVG present. Skips the
                                       hd/mom/atr condition, applies spike-specific
                                       penalty (DERIV_BOOM_CRASH_NO_FVG_PENALTY).
  DERIV_BOOM_CRASH_NO_FVG_PENALTY     float (default 0.20) — penalty when escape path
                                       is env-forced. Lower than generic escape (0.50).
  DERIV_BOOM_CRASH_CALM_EFFECTIVE_MIN float (default 5.25, min 5.25) — score floor for
                                       BOOM/CRASH in calm Grade-C territory.
                                       Math: 5.48 base − 0.20 penalty = 5.28 ≥ 5.25 → PASS.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Triggers .env load via the existing config module (idempotent).
from src.utils.config import BASE_DIR  # noqa: F401  — keeps env loaded


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return tuple(
        dict.fromkeys(item.strip().upper() for item in raw.split(",") if item.strip())
    )


@dataclass(slots=True)
class DerivSettings:
    """Immutable configuration snapshot for the Deriv daemon."""

    # ── Auth ────────────────────────────────────────────────────────────────
    api_token: str
    app_id: str
    account_id: str
    user_id: str  # UUID of the PAMM beneficiary user in PostgreSQL

    # ── Trading universe ────────────────────────────────────────────────────
    symbols: tuple[str, ...]
    dry_run: bool

    # ── Sizing & risk ───────────────────────────────────────────────────────
    bankroll_usdt: float
    risk_per_trade_pct: float
    max_open_contracts: int
    min_score: float
    max_daily_dd_pct: float
    loss_streak_lockout: int
    lockout_hours: int
    min_stake_usdt: float
    max_spread_pct: float

    # ── Contract specifics ──────────────────────────────────────────────────
    contract_duration_sec: int
    multiplier: int
    take_profit_pct: float
    stop_loss_pct: float

    # ── Engine cadence ──────────────────────────────────────────────────────
    poll_seconds: float

    # ── PAMM webhook (re-uses the Binance webhook URL + secret) ─────────────
    pamm_webhook_url: str
    webhook_secret: str

    # ── Persistence (broker-scoped JSON files) ──────────────────────────────
    logs_dir: Path = field(default_factory=lambda: Path(os.getenv("LOGS_DIR", str(BASE_DIR / "logs"))).expanduser())

    # ── Telegram (optional, re-uses Binance creds if unset) ─────────────────
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── BOOM/CRASH spike-gate (Phase 12) ─────────────────────────────────────
    # These mirror the os.getenv() calls in deriv_risk.py so the effective values
    # are visible in one place and logged at boot.
    boom_crash_escape_valve: bool = False          # DERIV_BOOM_CRASH_ESCAPE_VALVE
    boom_crash_no_fvg_penalty: float = 0.20        # DERIV_BOOM_CRASH_NO_FVG_PENALTY
    boom_crash_calm_effective_min: float = 5.25    # DERIV_BOOM_CRASH_CALM_EFFECTIVE_MIN
    # AI gate thresholds — separate so BOOM/CRASH can use a lower bar than R_*
    # BOOM/CRASH have their own protection (spike_timeout + broker SL) so a
    # lower AI gate is mathematically justified.  R_* retain the stricter 7.5.
    boom_crash_ai_min_score: float = 6.00          # DERIV_BOOM_CRASH_AI_MIN_SCORE
    r_indices_ai_min_score: float = 7.50           # DERIV_R_INDICES_AI_MIN_SCORE

    # ── Derived paths ───────────────────────────────────────────────────────
    @property
    def state_file(self) -> Path:
        return self.logs_dir / "deriv_state.json"

    @property
    def open_contracts_file(self) -> Path:
        return self.logs_dir / "deriv_open_contracts.json"

    @property
    def closed_contracts_file(self) -> Path:
        return self.logs_dir / "deriv_closed_contracts.json"

    @property
    def status_file(self) -> Path:
        return self.logs_dir / "deriv_status.json"

    @property
    def control_file(self) -> Path:
        return self.logs_dir / "deriv_control.json"

    @property
    def lockout_file(self) -> Path:
        return self.logs_dir / "deriv_lockout.json"


def load_deriv_settings() -> DerivSettings:
    """Build a `DerivSettings` from environment. Fails closed on missing token."""
    token = os.getenv("DERIV_API_TOKEN", "").strip()
    app_id = os.getenv("DERIV_APP_ID", "1089").strip()
    account_id = os.getenv("DERIV_ACCOUNT_ID", "").strip()
    user_id = os.getenv("DERIV_USER_ID", "").strip()

    return DerivSettings(
        api_token=token,
        app_id=app_id,
        account_id=account_id,
        user_id=user_id,
        symbols=_get_tuple(
            "DERIV_SYMBOLS",
            # Default universe: volatility indices + BOOM/CRASH (full active family)
            # Override via DERIV_SYMBOLS env var (comma-separated) in Coolify.
            # BOOM300/CRASH300 are intentionally omitted until ≥20 trades confirm edge.
            ("R_100", "R_75", "R_50",
             "BOOM1000", "CRASH1000",
             "BOOM500",  "CRASH500",
             "BOOM900",  "CRASH900",
             "BOOM600",  "CRASH600"),
        ),
        dry_run=_get_bool("DERIV_DRY_RUN", True),
        bankroll_usdt=_get_float("DERIV_BANKROLL_USDT", 50.0),
        risk_per_trade_pct=_get_float("DERIV_RISK_PER_TRADE_PCT", 0.01),
        max_open_contracts=_get_int("DERIV_MAX_OPEN_CONTRACTS", 3),
        min_score=_get_float("DERIV_MIN_SCORE", 6.0),
        max_daily_dd_pct=_get_float("DERIV_MAX_DAILY_DD_PCT", 0.02),
        loss_streak_lockout=_get_int("DERIV_LOSS_STREAK_LOCKOUT", 3),
        lockout_hours=_get_int("DERIV_LOCKOUT_HOURS", 12),
        min_stake_usdt=_get_float("DERIV_MIN_STAKE_USDT", 10.0),
        max_spread_pct=_get_float("DERIV_MAX_SPREAD_PCT", 0.0010),
        contract_duration_sec=_get_int("DERIV_CONTRACT_DURATION_SEC", 300),
        multiplier=_get_int("DERIV_MULTIPLIER", 200),
        take_profit_pct=_get_float("DERIV_TAKE_PROFIT_PCT", 0.012),
        stop_loss_pct=_get_float("DERIV_STOP_LOSS_PCT", 0.008),
        poll_seconds=_get_float("DERIV_POLL_SECONDS", 1.0),
        pamm_webhook_url=os.getenv("PAMM_WEBHOOK_URL", "").strip(),
        webhook_secret=os.getenv("WEBHOOK_SECRET", "").strip(),
        telegram_enabled=_get_bool("TELEGRAM_ENABLED", False),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        boom_crash_escape_valve=_get_bool("DERIV_BOOM_CRASH_ESCAPE_VALVE", False),
        boom_crash_no_fvg_penalty=_get_float("DERIV_BOOM_CRASH_NO_FVG_PENALTY", 0.20),
        boom_crash_calm_effective_min=max(
            _get_float("DERIV_BOOM_CRASH_CALM_EFFECTIVE_MIN", 5.25), 5.25
        ),
        boom_crash_ai_min_score=max(
            _get_float("DERIV_BOOM_CRASH_AI_MIN_SCORE", 6.00), 5.25
        ),
        r_indices_ai_min_score=max(
            _get_float("DERIV_R_INDICES_AI_MIN_SCORE", 7.50), 5.25
        ),
    )
