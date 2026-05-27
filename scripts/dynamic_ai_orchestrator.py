"""
AI dynamic orchestrator for Deriv symbols.

Purpose:
- Read recent telemetry every N seconds.
- Ask an LLM for per-symbol runtime thresholds.
- Enforce hard guardrails locally.
- Update PostgreSQL table dynamic_symbol_config.

Run:
    DATABASE_URL=postgresql://... \
    DYNAMIC_AI_API_KEY=... \
    DYNAMIC_AI_MODEL=gpt-4o-mini \
    python -m scripts.dynamic_ai_orchestrator
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


LOG = logging.getLogger("dynamic_ai_orchestrator")


def _sanitize_env_quotes() -> None:
    """Strip a single pair of wrapping single/double quotes from env values.

    Coolify's ``is_literal`` mode wraps numeric env values like ``6.8`` as
    ``'6.8'``, which breaks ``float(os.getenv(...))`` calls. We normalise once
    at import time so the rest of the module can read raw numerics safely.
    """
    for key, value in list(os.environ.items()):
        if not isinstance(value, str) or len(value) < 2:
            continue
        first, last = value[0], value[-1]
        if first == last and first in ("'", '"'):
            stripped = value[1:-1]
            if first not in stripped:
                os.environ[key] = stripped


_sanitize_env_quotes()

SYMBOLS = [
    "BOOM1000", "BOOM900", "BOOM600", "BOOM500",
    "CRASH1000", "CRASH900", "CRASH600", "CRASH500",
]

SCORE_MIN_GUARDRAIL = float(os.getenv("DYNAMIC_AI_SCORE_MIN_GUARDRAIL", "5.5") or 5.5)
SCORE_MAX_GUARDRAIL = float(os.getenv("DYNAMIC_AI_SCORE_MAX_GUARDRAIL", "9.2") or 9.2)
SCORE_MAX_DB_COMPAT_FALLBACK = float(
    os.getenv("DYNAMIC_AI_SCORE_MAX_DB_COMPAT_FALLBACK", str(SCORE_MAX_GUARDRAIL))
    or SCORE_MAX_GUARDRAIL
)
TICK_RECENT_WINDOW_SEC = max(
    900,
    int(os.getenv("DYNAMIC_AI_TICK_RECENT_WINDOW_SEC", "7200") or 7200),
)
TICK_BASELINE_WINDOW_SEC = max(
    TICK_RECENT_WINDOW_SEC + 1800,
    int(os.getenv("DYNAMIC_AI_TICK_BASELINE_WINDOW_SEC", "21600") or 21600),
)
TELEMETRY_MICRO_WINDOW_SEC = max(
    300,
    int(os.getenv("DYNAMIC_AI_MICRO_WINDOW_SEC", "900") or 900),
)
TELEMETRY_LOOKBACK_SEC_DEFAULT = max(
    3600,
    TICK_BASELINE_WINDOW_SEC,
    int(
        os.getenv(
            "DYNAMIC_AI_TELEMETRY_LOOKBACK_SEC",
            os.getenv("DYNAMIC_AI_LOOKBACK_SEC", "21600"),
        )
        or 21600
    ),
)
SPIKE_PREFILTER_MIN_TICKS = max(
    50,
    int(
        os.getenv(
            "DYNAMIC_AI_SPIKE_PREFILTER_MIN_TICKS",
            os.getenv(
                "DYNAMIC_AI_SPIKE_PREFILTER_FLOOR_TICKS",
                os.getenv("DYNAMIC_AI_SPIKE_PREFILTER_FLOOR_SEC", "90"),
            ),
        )
        or 90
    ),
)
SPIKE_PREFILTER_MAX_TICKS = max(
    500,
    int(
        os.getenv(
            "DYNAMIC_AI_SPIKE_PREFILTER_MAX_TICKS",
            os.getenv("DYNAMIC_AI_SPIKE_PREFILTER_CAP_TICKS", "2500"),
        )
        or 2500
    ),
)
SPIKE_PREFILTER_MAX_STEP_TICKS = max(
    20,
    int(os.getenv("DYNAMIC_AI_SPIKE_PREFILTER_MAX_STEP_TICKS", "180") or 180),
)
SPIKE_PREFILTER_MAX_CYCLE_RATIO = max(
    0.30,
    min(float(os.getenv("DYNAMIC_AI_SPIKE_PREFILTER_MAX_CYCLE_RATIO", "0.60") or 0.60), 0.95),
)
TICK_WINDOW_MIN_SAMPLES = max(
    2,
    int(os.getenv("DYNAMIC_AI_TICK_WINDOW_MIN_SAMPLES", "4") or 4),
)
TICK_TARGET_BLEND_MEAN = max(
    0.0,
    min(float(os.getenv("DYNAMIC_AI_TICK_TARGET_BLEND_MEAN", "0.20") or 0.20), 0.80),
)
ACCEL_RATIO_SLOW_THRESHOLD = max(
    1.01,
    float(os.getenv("DYNAMIC_AI_ACCEL_RATIO_SLOW_THRESHOLD", "1.20") or 1.20),
)
ACCEL_RATIO_FAST_THRESHOLD = min(
    ACCEL_RATIO_SLOW_THRESHOLD - 0.01,
    max(0.01, float(os.getenv("DYNAMIC_AI_ACCEL_RATIO_FAST_THRESHOLD", "0.80") or 0.80)),
)
ACCEL_SLOW_TARGET_MULT = max(
    0.10,
    min(float(os.getenv("DYNAMIC_AI_ACCEL_SLOW_TARGET_MULT", "0.75") or 0.75), 1.50),
)
ACCEL_FAST_TARGET_MULT = max(
    0.10,
    min(float(os.getenv("DYNAMIC_AI_ACCEL_FAST_TARGET_MULT", "0.40") or 0.40), 1.00),
)
ACCEL_FAST_SCORE_RELAX = max(
    0.10,
    min(float(os.getenv("DYNAMIC_AI_ACCEL_FAST_SCORE_RELAX", "0.15") or 0.15), 0.20),
)
ACCEL_SLOW_SCORE_BONUS = max(
    0.0,
    min(float(os.getenv("DYNAMIC_AI_ACCEL_SLOW_SCORE_BONUS", "0.10") or 0.10), 0.60),
)
SCORE_RECOVERY_BASE = max(
    SCORE_MIN_GUARDRAIL,
    min(float(os.getenv("DYNAMIC_AI_SCORE_RECOVERY_BASE", "6.80") or 6.80), SCORE_MAX_GUARDRAIL),
)
SCORE_RECOVERY_STEP_STABLE = max(
    0.10,
    min(float(os.getenv("DYNAMIC_AI_SCORE_RECOVERY_STEP_STABLE", "0.80") or 0.80), 1.20),
)
SYMBOL_NEGATIVE_THRESHOLD = float(os.getenv("DERIV_SYMBOL_NEGATIVE_THRESHOLD", "-2.0") or -2.0)
MULTISPIKE_BUFFER_TICKS_DEFAULT = max(
    5,
    int(os.getenv("DERIV_MULTISPIKE_BUFFER_TICKS_DEFAULT", "45") or 45),
)
MULTISPIKE_BUFFER_TICKS_FAST = max(
    5,
    int(os.getenv("DERIV_MULTISPIKE_BUFFER_TICKS_FAST", "60") or 60),
)
MULTISPIKE_BUFFER_TICKS_SLOW = max(
    5,
    int(os.getenv("DERIV_MULTISPIKE_BUFFER_TICKS_SLOW", "10") or 10),
)
MULTISPIKE_RETENTION_PCT_DEFAULT = max(
    0.30,
    min(float(os.getenv("DERIV_MULTISPIKE_RETENTION_PCT_DEFAULT", "0.70") or 0.70), 0.95),
)
MULTISPIKE_RETENTION_PCT_FAST = max(
    0.30,
    min(float(os.getenv("DERIV_MULTISPIKE_RETENTION_PCT_FAST", "0.50") or 0.50), 0.95),
)
MULTISPIKE_RETENTION_PCT_SLOW = max(
    0.30,
    min(float(os.getenv("DERIV_MULTISPIKE_RETENTION_PCT_SLOW", "0.85") or 0.85), 0.95),
)


def _parse_symbol_float_map(raw: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for chunk in str(raw or "").split(","):
        item = chunk.strip()
        if not item or ":" not in item:
            continue
        sym_raw, val_raw = item.split(":", 1)
        sym = sym_raw.strip().upper()
        if not sym:
            continue
        try:
            out[sym] = float(val_raw.strip())
        except Exception:
            continue
    return out


def _parse_symbol_set(raw: str) -> set[str]:
    out: set[str] = set()
    for item in (raw or "").split(","):
        sym = item.strip().upper()
        if sym:
            out.add(sym)
    return out


SYMBOL_SCORE_FLOOR_MAP: dict[str, float] = {
    "BOOM500": float(os.getenv("DYNAMIC_AI_BOOM500_SCORE_FLOOR", "6.8") or 6.8),
    "BOOM600": float(os.getenv("DYNAMIC_AI_BOOM600_SCORE_FLOOR", "5.8") or 5.8),
    "BOOM900": float(os.getenv("DYNAMIC_AI_BOOM900_SCORE_FLOOR", "5.8") or 5.8),
    "CRASH600": float(os.getenv("DYNAMIC_AI_CRASH600_SCORE_FLOOR", "5.8") or 5.8),
    "CRASH900": float(os.getenv("DYNAMIC_AI_CRASH900_SCORE_FLOOR", "5.8") or 5.8),
}
SYMBOL_SCORE_FLOOR_MAP.update(
    _parse_symbol_float_map(os.getenv("DYNAMIC_AI_SYMBOL_SCORE_FLOOR_MAP", ""))
)


def _symbol_score_floor(symbol: str) -> float:
    sym = str(symbol or "").upper()
    base = float(SYMBOL_SCORE_FLOOR_MAP.get(sym, SCORE_MIN_GUARDRAIL))
    return max(SCORE_MIN_GUARDRAIL, min(base, SCORE_MAX_GUARDRAIL))


def _symbol_cycle_ticks(symbol: str) -> int | None:
    sym = str(symbol or "").upper()
    match = re.search(r"(\d+)$", sym)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except Exception:
        return None
    return value if value > 0 else None


QUARANTINE_MEMORY: dict[str, bool] = {}
ZERO_PEAK_FLOOR_BY_SYMBOL = {
    "BOOM500": 60,
    "CRASH500": 60,
    "CRASH600": 60,
}
STRICT_DISABLE_SYMBOLS = _parse_symbol_set(
    os.getenv("DYNAMIC_AI_STRICT_DISABLE_SYMBOLS", "BOOM500")
)
STRICT_DISABLE_MIN_CONTRACTS = max(
    1,
    int(os.getenv("DYNAMIC_AI_STRICT_DISABLE_MIN_CONTRACTS", "4") or 4),
)

# Activity re-enable policy:
# - Re-enable only after sustained recovery across N sidecar cycles.
# - Once re-enabled, keep temporary hard thresholds while the symbol stabilizes.
ACTIVITY_RECOVERY_CYCLES = max(
    3,
    int(os.getenv("DYNAMIC_AI_ACTIVITY_RECOVERY_CYCLES", "5") or 5),
)
ACTIVITY_STABILIZATION_CYCLES = max(
    2,
    int(os.getenv("DYNAMIC_AI_ACTIVITY_STABILIZATION_CYCLES", "5") or 5),
)
ACTIVITY_STABILIZATION_SCORE_BONUS = max(
    0.0,
    float(os.getenv("DYNAMIC_AI_ACTIVITY_STABILIZATION_SCORE_BONUS", "0.8") or 0.8),
)
ACTIVITY_STABILIZATION_RELAX_STEP = max(
    0.0,
    float(os.getenv("DYNAMIC_AI_ACTIVITY_STABILIZATION_RELAX_STEP", "0.2") or 0.2),
)
ACTIVITY_STABILIZATION_GRACE_FLOOR = max(
    0,
    min(int(os.getenv("DYNAMIC_AI_ACTIVITY_STABILIZATION_GRACE_FLOOR", "90") or 90), 120),
)

ACTIVITY_POLICY_MEMORY: dict[str, dict[str, Any]] = {}

# Short-term memory to prevent regime oscillation.
STATE_MEMORY: dict[str, dict[str, Any]] = {}
MIN_STATE_LIFETIME_SEC = max(
    480,
    min(int(os.getenv("DYNAMIC_AI_MIN_STATE_LIFETIME_SEC", "600") or 600), 720),
)
SCORE_HYSTERESIS_DEADBAND = max(
    0.05,
    min(float(os.getenv("DYNAMIC_AI_SCORE_HYSTERESIS_DEADBAND", "0.20") or 0.20), 0.80),
)
SCORE_HYSTERESIS_FORCE_DELTA = max(
    SCORE_HYSTERESIS_DEADBAND,
    min(float(os.getenv("DYNAMIC_AI_SCORE_HYSTERESIS_FORCE_DELTA", "0.65") or 0.65), 1.50),
)
SCORE_MAX_STEP_PER_CYCLE = max(
    0.10,
    min(float(os.getenv("DYNAMIC_AI_SCORE_MAX_STEP_PER_CYCLE", "0.40") or 0.40), 1.20),
)
SCORE_MIN_LIFETIME_SEC = max(
    120,
    int(os.getenv("DYNAMIC_AI_SCORE_MIN_LIFETIME_SEC", "420") or 420),
)
PREFILTER_HYSTERESIS_DEADBAND_TICKS = max(
    5,
    int(os.getenv("DYNAMIC_AI_PREFILTER_HYSTERESIS_DEADBAND_TICKS", "25") or 25),
)
PREFILTER_HYSTERESIS_FORCE_DELTA_TICKS = max(
    PREFILTER_HYSTERESIS_DEADBAND_TICKS,
    int(os.getenv("DYNAMIC_AI_PREFILTER_HYSTERESIS_FORCE_DELTA_TICKS", "90") or 90),
)
PREFILTER_MAX_STEP_PER_CYCLE_TICKS = max(
    5,
    min(
        int(os.getenv("DYNAMIC_AI_PREFILTER_MAX_STEP_PER_CYCLE_TICKS", "70") or 70),
        SPIKE_PREFILTER_MAX_STEP_TICKS,
    ),
)
PREFILTER_MIN_LIFETIME_SEC = max(
    120,
    int(os.getenv("DYNAMIC_AI_PREFILTER_MIN_LIFETIME_SEC", "420") or 420),
)
PATTERN_MEMORY_ENABLED = os.getenv("DYNAMIC_AI_PATTERN_MEMORY_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
PATTERN_MEMORY_LOOKBACK = max(
    80,
    int(os.getenv("DYNAMIC_AI_PATTERN_MEMORY_LOOKBACK", "500") or 500),
)

# --- Market Phase intelligence (per symbol × UTC hour) ---------------------
# Adds a third layer on top of activity policy + tick-window acceleration: a
# session baseline learned across days that tells the sidecar when a symbol is
# performing *below* its own historical norm for the current hour-of-day and
# should be put into CAUTION/DECEL/DEAD even if intra-session ratios look OK.
from scripts import market_phase as MP  # noqa: E402

MARKET_PHASE_ENABLED = os.getenv("MARKET_PHASE_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
MARKET_PHASE_CAUTION_SCORE_BUMP = max(
    0.0,
    min(float(os.getenv("MARKET_PHASE_CAUTION_SCORE_BUMP", "0.45") or 0.45), 1.50),
)
MARKET_PHASE_DECEL_SCORE_BUMP = max(
    MARKET_PHASE_CAUTION_SCORE_BUMP,
    min(float(os.getenv("MARKET_PHASE_DECEL_SCORE_BUMP", "0.85") or 0.85), 2.00),
)
MARKET_PHASE_DEAD_SCORE_BUMP = max(
    MARKET_PHASE_DECEL_SCORE_BUMP,
    min(float(os.getenv("MARKET_PHASE_DEAD_SCORE_BUMP", "1.30") or 1.30), 2.50),
)
MARKET_PHASE_ACCEL_SCORE_RELAX = max(
    0.0,
    min(float(os.getenv("MARKET_PHASE_ACCEL_SCORE_RELAX", "0.20") or 0.20), 0.50),
)
MARKET_PHASE_DEAD_DISABLES = os.getenv("MARKET_PHASE_DEAD_DISABLES", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
MARKET_PHASE_DECEL_GRACE_BUMP = max(
    0,
    min(int(os.getenv("MARKET_PHASE_DECEL_GRACE_BUMP", "30") or 30), 120),
)


@dataclass
class SymbolCfg:
    regime: str
    spike_pre_filter_target: int
    zero_peak_grace_sec: int
    score_min_override: float
    is_active: bool = True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp_cfg(symbol: str, cfg: SymbolCfg) -> SymbolCfg:
    sym = str(symbol or "").upper()
    regime = str(cfg.regime or "NORMAL").upper()
    if regime not in {"FAST", "SLOW", "NORMAL"}:
        regime = "NORMAL"
    zero_peak_floor = ZERO_PEAK_FLOOR_BY_SYMBOL.get(sym, 0)
    score_floor = _symbol_score_floor(sym)
    return SymbolCfg(
        regime=regime,
        spike_pre_filter_target=max(
            SPIKE_PREFILTER_MIN_TICKS,
            min(int(cfg.spike_pre_filter_target), SPIKE_PREFILTER_MAX_TICKS),
        ),
        zero_peak_grace_sec=max(zero_peak_floor, min(int(cfg.zero_peak_grace_sec), 120)),
        score_min_override=max(score_floor, min(float(cfg.score_min_override), SCORE_MAX_GUARDRAIL)),
        is_active=bool(cfg.is_active),
    )


def _parse_json_maybe_fenced(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_\-]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    match = re.search(r"\{.*\}", text, flags=re.S)
    if match:
        parsed = json.loads(match.group(0))
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("LLM response is not valid JSON object")


def _safe_ts(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return float(s)
        except Exception:
            pass
        s = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return None
    return None


def _cfg_to_dict(cfg: SymbolCfg) -> dict[str, Any]:
    return {
        "regime": cfg.regime,
        "spike_pre_filter_target": cfg.spike_pre_filter_target,
        "zero_peak_grace_sec": cfg.zero_peak_grace_sec,
        "score_min_override": round(float(cfg.score_min_override), 3),
        "is_active": bool(cfg.is_active),
    }


def _cfg_diff_items(old_cfg: SymbolCfg, new_cfg: SymbolCfg) -> list[str]:
    diffs: list[str] = []
    keys = (
        "spike_pre_filter_target",
        "zero_peak_grace_sec",
        "score_min_override",
        "regime",
        "is_active",
    )
    old_map = _cfg_to_dict(old_cfg)
    new_map = _cfg_to_dict(new_cfg)
    for key in keys:
        old_val = old_map.get(key)
        new_val = new_map.get(key)
        if old_val != new_val:
            diffs.append(f"{key}: {old_val} -> {new_val}")
    return diffs


def _cfg_changed(old_cfg: SymbolCfg, new_cfg: SymbolCfg) -> bool:
    return bool(_cfg_diff_items(old_cfg, new_cfg))


def _append_diff_jsonl(diff_log_path: Path, payload: dict[str, Any]) -> None:
    try:
        diff_log_path.parent.mkdir(parents=True, exist_ok=True)
        with diff_log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except Exception as exc:  # noqa: BLE001
        LOG.warning("[dynamic-ai] could not append diff log: %s", exc)


def _seed_state_memory(current_cfg: dict[str, SymbolCfg]) -> None:
    now_ts = datetime.now(timezone.utc).timestamp()
    for sym, cfg in current_cfg.items():
        STATE_MEMORY.setdefault(
            sym,
            {
                "regime": cfg.regime,
                "config": cfg,
                "last_changed": now_ts,
                "last_score_changed": now_ts,
                "last_spf_changed": now_ts,
            },
        )


def _default_activity_state() -> dict[str, Any]:
    return {
        "recovery_streak": 0,
        "stabilization_left": 0,
        "strict_bonus": 0.0,
    }


def _seed_activity_memory(current_cfg: dict[str, SymbolCfg]) -> None:
    for sym, cfg in current_cfg.items():
        state = ACTIVITY_POLICY_MEMORY.setdefault(sym, _default_activity_state())
        state["recovery_streak"] = int(state.get("recovery_streak") or 0)
        state["stabilization_left"] = int(state.get("stabilization_left") or 0)
        state["strict_bonus"] = float(state.get("strict_bonus") or 0.0)
        if not cfg.is_active:
            # Inactive symbols should not carry stale post-reactivation state.
            state["stabilization_left"] = 0
            state["strict_bonus"] = 0.0


def _load_activity_memory(path: Path) -> None:
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return
        for sym, raw_state in payload.items():
            sym_u = str(sym or "").upper()
            if sym_u not in SYMBOLS or not isinstance(raw_state, dict):
                continue
            ACTIVITY_POLICY_MEMORY[sym_u] = {
                "recovery_streak": max(0, int(raw_state.get("recovery_streak") or 0)),
                "stabilization_left": max(0, int(raw_state.get("stabilization_left") or 0)),
                "strict_bonus": max(0.0, float(raw_state.get("strict_bonus") or 0.0)),
            }
    except Exception as exc:  # noqa: BLE001
        LOG.warning("[dynamic-ai] could not load activity memory: %s", exc)


def _save_activity_memory(path: Path) -> None:
    try:
        payload: dict[str, dict[str, Any]] = {}
        for sym in SYMBOLS:
            state = ACTIVITY_POLICY_MEMORY.get(sym) or _default_activity_state()
            payload[sym] = {
                "recovery_streak": max(0, int(state.get("recovery_streak") or 0)),
                "stabilization_left": max(0, int(state.get("stabilization_left") or 0)),
                "strict_bonus": round(max(0.0, float(state.get("strict_bonus") or 0.0)), 4),
            }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        LOG.warning("[dynamic-ai] could not save activity memory: %s", exc)


def apply_temporal_smoothing(symbol: str, new_config: SymbolCfg) -> tuple[SymbolCfg, bool]:
    now_ts = datetime.now(timezone.utc).timestamp()
    sym = str(symbol or "").upper()

    if sym not in STATE_MEMORY:
        STATE_MEMORY[sym] = {
            "regime": new_config.regime,
            "config": new_config,
            "last_changed": now_ts,
            "last_score_changed": now_ts,
            "last_spf_changed": now_ts,
        }
        return new_config, True

    mem = STATE_MEMORY[sym]
    previous_cfg = mem.get("config")
    if not isinstance(previous_cfg, SymbolCfg):
        previous_cfg = _clamp_cfg(sym, SymbolCfg("NORMAL", 280, 0, 7.0, True))

    previous_regime = str(mem.get("regime") or previous_cfg.regime or "NORMAL").upper()
    next_regime = str(new_config.regime or previous_regime).upper()
    time_since_last_change = now_ts - float(mem.get("last_changed") or now_ts)
    last_score_changed = float(mem.get("last_score_changed") or mem.get("last_changed") or now_ts)
    last_spf_changed = float(mem.get("last_spf_changed") or mem.get("last_changed") or now_ts)

    adjusted_score = float(new_config.score_min_override)
    score_delta = adjusted_score - float(previous_cfg.score_min_override)
    if abs(score_delta) < SCORE_HYSTERESIS_DEADBAND:
        adjusted_score = float(previous_cfg.score_min_override)
    elif (now_ts - last_score_changed) < SCORE_MIN_LIFETIME_SEC and abs(score_delta) < SCORE_HYSTERESIS_FORCE_DELTA:
        LOG.info(
            "[dynamic-ai][HYSTERESIS] %s score hold %.2f -> %.2f (age=%ss force=%.2f)",
            sym,
            float(previous_cfg.score_min_override),
            float(new_config.score_min_override),
            int(now_ts - last_score_changed),
            SCORE_HYSTERESIS_FORCE_DELTA,
        )
        adjusted_score = float(previous_cfg.score_min_override)
    else:
        if abs(score_delta) > SCORE_MAX_STEP_PER_CYCLE:
            adjusted_score = float(previous_cfg.score_min_override) + (
                SCORE_MAX_STEP_PER_CYCLE if score_delta > 0 else -SCORE_MAX_STEP_PER_CYCLE
            )

    adjusted_spf = int(new_config.spike_pre_filter_target)
    spf_delta = adjusted_spf - int(previous_cfg.spike_pre_filter_target)
    if abs(spf_delta) < PREFILTER_HYSTERESIS_DEADBAND_TICKS:
        adjusted_spf = int(previous_cfg.spike_pre_filter_target)
    elif (now_ts - last_spf_changed) < PREFILTER_MIN_LIFETIME_SEC and abs(spf_delta) < PREFILTER_HYSTERESIS_FORCE_DELTA_TICKS:
        LOG.info(
            "[dynamic-ai][HYSTERESIS] %s prefilter hold %d -> %d (age=%ss force=%dt)",
            sym,
            int(previous_cfg.spike_pre_filter_target),
            int(new_config.spike_pre_filter_target),
            int(now_ts - last_spf_changed),
            PREFILTER_HYSTERESIS_FORCE_DELTA_TICKS,
        )
        adjusted_spf = int(previous_cfg.spike_pre_filter_target)
    else:
        if abs(spf_delta) > PREFILTER_MAX_STEP_PER_CYCLE_TICKS:
            adjusted_spf = int(previous_cfg.spike_pre_filter_target) + (
                PREFILTER_MAX_STEP_PER_CYCLE_TICKS if spf_delta > 0 else -PREFILTER_MAX_STEP_PER_CYCLE_TICKS
            )

    candidate_cfg = _clamp_cfg(
        sym,
        SymbolCfg(
            regime=next_regime,
            spike_pre_filter_target=adjusted_spf,
            zero_peak_grace_sec=new_config.zero_peak_grace_sec,
            score_min_override=adjusted_score,
            is_active=new_config.is_active,
        ),
    )

    if next_regime == previous_regime:
        mem["config"] = candidate_cfg
        if abs(candidate_cfg.score_min_override - float(previous_cfg.score_min_override)) >= 1e-9:
            mem["last_score_changed"] = now_ts
        if int(candidate_cfg.spike_pre_filter_target) != int(previous_cfg.spike_pre_filter_target):
            mem["last_spf_changed"] = now_ts
        return candidate_cfg, True

    emergency_change = (not bool(candidate_cfg.is_active)) or next_regime == "DORMANT"
    if time_since_last_change < MIN_STATE_LIFETIME_SEC and not emergency_change:
        fallback = _clamp_cfg(
            sym,
            SymbolCfg(
                regime=previous_regime,
                spike_pre_filter_target=candidate_cfg.spike_pre_filter_target,
                zero_peak_grace_sec=candidate_cfg.zero_peak_grace_sec,
                score_min_override=candidate_cfg.score_min_override,
                is_active=candidate_cfg.is_active,
            ),
        )
        mem["config"] = fallback
        if abs(fallback.score_min_override - float(previous_cfg.score_min_override)) >= 1e-9:
            mem["last_score_changed"] = now_ts
        if int(fallback.spike_pre_filter_target) != int(previous_cfg.spike_pre_filter_target):
            mem["last_spf_changed"] = now_ts
        return fallback, False

    STATE_MEMORY[sym] = {
        "regime": next_regime,
        "config": candidate_cfg,
        "last_changed": now_ts,
        "last_score_changed": (
            now_ts
            if abs(candidate_cfg.score_min_override - float(previous_cfg.score_min_override)) >= 1e-9
            else last_score_changed
        ),
        "last_spf_changed": (
            now_ts
            if int(candidate_cfg.spike_pre_filter_target) != int(previous_cfg.spike_pre_filter_target)
            else last_spf_changed
        ),
    }
    return candidate_cfg, True


def _hour_bucket(ts: float | None) -> str:
    if not isinstance(ts, (int, float)):
        return "UNKNOWN"
    hour = datetime.fromtimestamp(float(ts), tz=timezone.utc).hour
    if 0 <= hour < 6:
        return "NIGHT"
    if 6 <= hour < 12:
        return "MORNING"
    if 12 <= hour < 18:
        return "ACTIVE"
    return "SLOW"


def _summary_from_pnls(pnls: list[float]) -> dict[str, float | int]:
    n = len(pnls)
    wins = sum(1 for x in pnls if x > 0)
    ev = (sum(pnls) / n) if n else 0.0
    return {
        "n": n,
        "win_rate": pct(wins, n),
        "ev": ev,
    }


def _apply_smoothing_all(candidate_cfg: dict[str, SymbolCfg]) -> tuple[dict[str, SymbolCfg], int]:
    smoothed: dict[str, SymbolCfg] = {}
    blocked_regime_flips = 0
    for sym in SYMBOLS:
        candidate = candidate_cfg[sym]
        final_cfg, approved = apply_temporal_smoothing(sym, candidate)
        if not approved and final_cfg.regime != candidate.regime:
            blocked_regime_flips += 1
            LOG.info(
                "[dynamic-ai][HYSTERESIS] %s rejected regime flip %s -> %s (cooldown=%ss)",
                sym,
                final_cfg.regime,
                candidate.regime,
                MIN_STATE_LIFETIME_SEC,
            )
        smoothed[sym] = final_cfg
    return smoothed, blocked_regime_flips


def _load_json(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _build_telemetry_from_logs(logs_dir: Path, lookback_sec: int = TELEMETRY_LOOKBACK_SEC_DEFAULT) -> dict[str, Any]:
    spikes = _load_json(logs_dir / "deriv_spike_events.json")
    closed = _load_json(logs_dir / "deriv_closed_contracts.json")
    ai_decisions = _load_json(logs_dir / "deriv_ai_decisions.json")
    market_ctx = _load_json(logs_dir / "deriv_market_context.json")
    lockout_raw: dict[str, Any] = {}
    lockout_path = logs_dir / "deriv_lockout.json"
    if lockout_path.exists():
        try:
            data = json.loads(lockout_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                lockout_raw = data
        except Exception:
            lockout_raw = {}

    lockout_sym_pnl = lockout_raw.get("symbol_pnl_window") if isinstance(lockout_raw.get("symbol_pnl_window"), dict) else {}
    lockout_sym_bonus = lockout_raw.get("symbol_score_bonus") if isinstance(lockout_raw.get("symbol_score_bonus"), dict) else {}
    lockout_sym_recent = lockout_raw.get("symbol_recent_pnls") if isinstance(lockout_raw.get("symbol_recent_pnls"), dict) else {}

    now_ts = time.time()
    min_ts = now_ts - lookback_sec
    baseline_window_sec = min(lookback_sec, TICK_BASELINE_WINDOW_SEC)
    baseline_min_ts = now_ts - baseline_window_sec
    recent_window_sec = min(baseline_window_sec, TICK_RECENT_WINDOW_SEC)
    recent_min_ts = now_ts - recent_window_sec
    micro_window_sec = min(lookback_sec, TELEMETRY_MICRO_WINDOW_SEC)
    micro_min_ts = now_ts - micro_window_sec

    spikes_by: dict[str, list[tuple[float, dict[str, Any]]]] = defaultdict(list)
    for s in spikes:
        sym = str(s.get("symbol") or "").upper()
        t = _safe_ts(s.get("ts") or s.get("timestamp") or s.get("iso"))
        if not sym or t is None or t < (min_ts - 3600):
            continue
        spikes_by[sym].append((t, s))
    for sym in spikes_by:
        spikes_by[sym].sort(key=lambda x: x[0])

    closes_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in closed:
        sym = str(c.get("symbol") or "").upper()
        opened = _safe_ts(c.get("opened_at_ts") or c.get("opened_at"))
        closed_ts = _safe_ts(c.get("closed_at_ts") or c.get("closed_at"))
        if not sym:
            continue
        if (opened is not None and opened >= min_ts) or (closed_ts is not None and closed_ts >= min_ts):
            closes_by[sym].append(c)

    ai_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in ai_decisions:
        sym = str(row.get("symbol") or "").upper()
        ts = _safe_ts(row.get("ts") or row.get("timestamp") or row.get("iso"))
        if not sym or ts is None or ts < min_ts:
            continue
        ai_by[sym].append(row)

    ctx_by: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in market_ctx:
        sym = str(row.get("symbol") or "").upper()
        ts = _safe_ts(row.get("ts") or row.get("timestamp") or row.get("iso"))
        ticks_since = row.get("ticks_since_last_spike")
        if not sym or ts is None or ticks_since is None:
            continue
        try:
            ticks_f = float(ticks_since)
        except Exception:
            continue
        if ticks_f < 0:
            continue
        if ts >= min_ts:
            ctx_by[sym].append((ts, ticks_f))
    for sym in ctx_by:
        ctx_by[sym].sort(key=lambda x: x[0])

    telemetry: dict[str, Any] = {}
    for sym in SYMBOLS:
        arr = spikes_by.get(sym, [])
        recent_macro = [(t, s) for t, s in arr if t >= min_ts]
        entered_macro = [s for _, s in recent_macro if bool(s.get("bot_entered"))]
        blocked_macro = [s for _, s in recent_macro if not bool(s.get("bot_entered"))]
        recent_micro = [(t, s) for t, s in arr if t >= micro_min_ts]
        entered_micro = [s for _, s in recent_micro if bool(s.get("bot_entered"))]
        blocked_micro = [s for _, s in recent_micro if not bool(s.get("bot_entered"))]

        # Pace split: last 5 min vs previous 10 min
        split_5m = now_ts - 300
        split_15m = now_ts - 900
        first = [1 for t, _ in arr if split_15m <= t < split_5m]
        second = [1 for t, _ in arr if split_5m <= t <= now_ts]
        rate_prev10 = (len(first) / 10.0) if first else 0.0
        rate_last5 = (len(second) / 5.0) if second else 0.0
        regime = "NORMAL"
        if rate_prev10 > 0 and rate_last5 >= rate_prev10 * 1.25:
            regime = "FAST"
        elif rate_prev10 > 0 and rate_last5 <= rate_prev10 * 0.75:
            regime = "SLOW"

        # Entry lag from closed contracts vs previous spike
        lags: list[float] = []
        lags_micro: list[float] = []
        early_exit_next_spike: list[float] = []
        early_exit_next_spike_micro: list[float] = []
        close_rows = closes_by.get(sym, [])
        close_rows_recent = [
            c for c in close_rows
            if (_safe_ts(c.get("opened_at_ts") or c.get("opened_at")) or 0.0) >= recent_min_ts
            or (_safe_ts(c.get("closed_at_ts") or c.get("closed_at")) or 0.0) >= recent_min_ts
        ]
        close_rows_micro = [
            c for c in close_rows
            if (_safe_ts(c.get("opened_at_ts") or c.get("opened_at")) or 0.0) >= micro_min_ts
            or (_safe_ts(c.get("closed_at_ts") or c.get("closed_at")) or 0.0) >= micro_min_ts
        ]
        spike_times = [t for t, _ in arr]
        spike_times_micro = [t for t, _ in arr if t >= micro_min_ts]
        for c in close_rows:
            ot = _safe_ts(c.get("opened_at_ts") or c.get("opened_at"))
            ct = _safe_ts(c.get("closed_at_ts") or c.get("closed_at"))
            if ot is not None:
                prev = [x for x in spike_times if x <= ot]
                if prev:
                    lag = ot - prev[-1]
                    if lag >= 0:
                        lags.append(lag)
            if ct is not None:
                nxt = [x for x in spike_times if x >= ct]
                if nxt:
                    dt = nxt[0] - ct
                    if dt >= 0:
                        early_exit_next_spike.append(dt)
        for c in close_rows_micro:
            ot = _safe_ts(c.get("opened_at_ts") or c.get("opened_at"))
            ct = _safe_ts(c.get("closed_at_ts") or c.get("closed_at"))
            if ot is not None:
                prev = [x for x in spike_times_micro if x <= ot]
                if prev:
                    lag = ot - prev[-1]
                    if lag >= 0:
                        lags_micro.append(lag)
            if ct is not None:
                nxt = [x for x in spike_times_micro if x >= ct]
                if nxt:
                    dt = nxt[0] - ct
                    if dt >= 0:
                        early_exit_next_spike_micro.append(dt)

        exits = Counter(str(c.get("exit_reason") or c.get("close_reason") or "") for c in close_rows)
        exits_micro = Counter(str(c.get("exit_reason") or c.get("close_reason") or "") for c in close_rows_micro)
        block_reasons = Counter(str(s.get("block_reason") or "none") for s in blocked_macro)
        block_reasons_micro = Counter(str(s.get("block_reason") or "none") for s in blocked_micro)
        pnl_values: list[float] = []
        pnl_values_micro: list[float] = []
        wins = 0
        wins_micro = 0
        for c in close_rows:
            try:
                pnl = float(c.get("realized_pnl_usdt") or 0.0)
            except Exception:
                pnl = 0.0
            pnl_values.append(pnl)
            if pnl > 0.0:
                wins += 1
        for c in close_rows_micro:
            try:
                pnl = float(c.get("realized_pnl_usdt") or 0.0)
            except Exception:
                pnl = 0.0
            pnl_values_micro.append(pnl)
            if pnl > 0.0:
                wins_micro += 1

        ai_rows = ai_by.get(sym, [])
        ai_rows_recent = [
            r for r in ai_rows
            if (_safe_ts(r.get("ts") or r.get("timestamp") or r.get("iso")) or 0.0) >= recent_min_ts
        ]
        ai_rows_micro = [
            r for r in ai_rows
            if (_safe_ts(r.get("ts") or r.get("timestamp") or r.get("iso")) or 0.0) >= micro_min_ts
        ]

        def _intervals_from_ctx(points: list[tuple[float, float]]) -> list[float]:
            if len(points) < 2:
                return []
            out: list[float] = []
            prev = points[0][1]
            for _, curr in points[1:]:
                if curr < prev and prev > 0:
                    out.append(prev)
                prev = curr
            return out

        ctx_points = ctx_by.get(sym, [])
        ctx_points_baseline = [p for p in ctx_points if p[0] >= baseline_min_ts]
        ctx_points_recent = [p for p in ctx_points if p[0] >= recent_min_ts]
        ctx_points_micro = [p for p in ctx_points if p[0] >= micro_min_ts]
        tick_intervals_6h = _intervals_from_ctx(ctx_points_baseline)
        tick_intervals_2h = _intervals_from_ctx(ctx_points_recent)
        tick_intervals_15m = _intervals_from_ctx(ctx_points_micro)
        ticks_since_spike_now = (
            int(round(ctx_points[-1][1])) if ctx_points else None
        )
        tick_p50_2h = statistics_median(tick_intervals_2h)
        tick_p50_6h = statistics_median(tick_intervals_6h)
        accel_ratio_2h_vs_6h = None
        if isinstance(tick_p50_2h, (int, float)) and isinstance(tick_p50_6h, (int, float)) and float(tick_p50_6h) > 0:
            accel_ratio_2h_vs_6h = float(tick_p50_2h) / float(tick_p50_6h)
        if isinstance(accel_ratio_2h_vs_6h, (int, float)) and float(accel_ratio_2h_vs_6h) < ACCEL_RATIO_FAST_THRESHOLD:
            multispike_buffer_suggested = MULTISPIKE_BUFFER_TICKS_FAST
            multispike_retention_suggested = MULTISPIKE_RETENTION_PCT_FAST
            multispike_mode_suggested = "CLUSTER"
        elif isinstance(accel_ratio_2h_vs_6h, (int, float)) and float(accel_ratio_2h_vs_6h) > ACCEL_RATIO_SLOW_THRESHOLD:
            multispike_buffer_suggested = MULTISPIKE_BUFFER_TICKS_SLOW
            multispike_retention_suggested = MULTISPIKE_RETENTION_PCT_SLOW
            multispike_mode_suggested = "CALM"
        else:
            multispike_buffer_suggested = MULTISPIKE_BUFFER_TICKS_DEFAULT
            multispike_retention_suggested = MULTISPIKE_RETENTION_PCT_DEFAULT
            multispike_mode_suggested = "BALANCED"
        ai_n = len(ai_rows)
        ai_n_recent = len(ai_rows_recent)
        ai_n_micro = len(ai_rows_micro)
        ai_approvals = sum(1 for r in ai_rows if bool(r.get("approved")))
        ai_approvals_recent = sum(1 for r in ai_rows_recent if bool(r.get("approved")))
        ai_approvals_micro = sum(1 for r in ai_rows_micro if bool(r.get("approved")))
        contracts_n = len(close_rows)
        contracts_n_recent = len(close_rows_recent)
        contracts_n_micro = len(close_rows_micro)
        pnl_values_recent: list[float] = []
        wins_recent = 0
        for c in close_rows_recent:
            try:
                pnl_recent = float(c.get("realized_pnl_usdt") or 0.0)
            except Exception:
                pnl_recent = 0.0
            pnl_values_recent.append(pnl_recent)
            if pnl_recent > 0.0:
                wins_recent += 1

        # Regime/time segmentation for all-weather calibration.
        time_bucket_pnls: dict[str, list[float]] = defaultdict(list)
        regime_bucket_pnls: dict[str, list[float]] = defaultdict(list)
        now_bucket = _hour_bucket(now_ts)
        for c in close_rows:
            t_close = _safe_ts(c.get("closed_at_ts") or c.get("closed_at") or c.get("opened_at_ts") or c.get("opened_at"))
            try:
                pnl_bucket = float(c.get("realized_pnl_usdt") or 0.0)
            except Exception:
                pnl_bucket = 0.0
            time_bucket_pnls[_hour_bucket(t_close)].append(pnl_bucket)
            sb = c.get("score_breakdown") if isinstance(c.get("score_breakdown"), dict) else {}
            regime_key = str(sb.get("market_regime") or sb.get("regime") or "UNKNOWN").upper()
            if regime_key not in {"FAST", "NORMAL", "SLOW"}:
                regime_key = "UNKNOWN"
            regime_bucket_pnls[regime_key].append(pnl_bucket)

        time_bucket_stats = {
            bucket: _summary_from_pnls(vals)
            for bucket, vals in time_bucket_pnls.items()
        }
        regime_bucket_stats = {
            bucket: _summary_from_pnls(vals)
            for bucket, vals in regime_bucket_pnls.items()
        }

        lookback_hours = round(float(lookback_sec) / 3600.0, 2)
        guard_pnl_window = float(lockout_sym_pnl.get(sym) or 0.0)
        guard_bonus = float(lockout_sym_bonus.get(sym) or 0.0)
        guard_recent_n = 0
        if isinstance(lockout_sym_recent.get(sym), list):
            guard_recent_n = len(lockout_sym_recent.get(sym) or [])

        telemetry[sym] = {
            "lookback_sec": lookback_sec,
            "lookback_hours": lookback_hours,
            "tick_window_recent_sec": recent_window_sec,
            "tick_window_baseline_sec": baseline_window_sec,
            "micro_window_sec": micro_window_sec,
            "spikes_15m": len(recent_micro),
            "entered_spikes_15m": len(entered_micro),
            "blocked_spikes_15m": len(blocked_micro),
            "entry_rate_pct": (len(entered_micro) / len(recent_micro) * 100.0) if recent_micro else 0.0,
            "spikes_6h": len(recent_macro),
            "entered_spikes_6h": len(entered_macro),
            "blocked_spikes_6h": len(blocked_macro),
            "entry_rate_pct_6h": (len(entered_macro) / len(recent_macro) * 100.0) if recent_macro else 0.0,
            "top_block_reasons": block_reasons.most_common(5),
            "top_block_reasons_15m": block_reasons_micro.most_common(5),
            "contracts_15m": contracts_n_micro,
            "win_rate_15m": pct(wins_micro, contracts_n_micro),
            "ev_per_trade_15m": (sum(pnl_values_micro) / contracts_n_micro) if contracts_n_micro else 0.0,
            "ai_decisions_15m": ai_n_micro,
            "ai_approval_rate_15m": pct(ai_approvals_micro, ai_n_micro),
            "contracts_2h": contracts_n_recent,
            "win_rate_2h": pct(wins_recent, contracts_n_recent),
            "ev_per_trade_2h": (sum(pnl_values_recent) / contracts_n_recent) if contracts_n_recent else 0.0,
            "ai_decisions_2h": ai_n_recent,
            "ai_approval_rate_2h": pct(ai_approvals_recent, ai_n_recent),
            "entry_lag_sec_median": statistics_median(lags_micro),
            "entry_lag_sec_p75": statistics_quantile(lags_micro, 0.75),
            "late_entry_ge120_pct": pct(sum(1 for x in lags_micro if x >= 120), len(lags_micro)),
            "zero_peak_exit_count": exits_micro.get("zero_peak_exit", 0),
            "next_spike_after_close_le80_pct": pct(sum(1 for x in early_exit_next_spike_micro if 0 < x <= 80), len(early_exit_next_spike_micro)),
            "contracts_6h": contracts_n,
            "win_rate_6h": pct(wins, contracts_n),
            "ev_per_trade_6h": (sum(pnl_values) / contracts_n) if contracts_n else 0.0,
            "current_hour_bucket": now_bucket,
            "time_bucket_stats": time_bucket_stats,
            "regime_bucket_stats": regime_bucket_stats,
            "ai_decisions_6h": ai_n,
            "ai_approval_rate_6h": pct(ai_approvals, ai_n),
            "tick_intervals_15m": len(tick_intervals_15m),
            "tick_interval_mean_15m": (sum(tick_intervals_15m) / len(tick_intervals_15m)) if tick_intervals_15m else None,
            "tick_interval_p25_15m": statistics_quantile(tick_intervals_15m, 0.25),
            "tick_interval_p50_15m": statistics_median(tick_intervals_15m),
            "tick_interval_p75_15m": statistics_quantile(tick_intervals_15m, 0.75),
            "tick_interval_p90_15m": statistics_quantile(tick_intervals_15m, 0.90),
            "tick_intervals_2h": len(tick_intervals_2h),
            "tick_interval_mean_2h": (sum(tick_intervals_2h) / len(tick_intervals_2h)) if tick_intervals_2h else None,
            "tick_interval_p25_2h": statistics_quantile(tick_intervals_2h, 0.25),
            "tick_interval_p50_2h": tick_p50_2h,
            "tick_interval_p75_2h": statistics_quantile(tick_intervals_2h, 0.75),
            "tick_interval_p90_2h": statistics_quantile(tick_intervals_2h, 0.90),
            "tick_intervals_6h": len(tick_intervals_6h),
            "tick_interval_mean_6h": (sum(tick_intervals_6h) / len(tick_intervals_6h)) if tick_intervals_6h else None,
            "tick_interval_p25_6h": statistics_quantile(tick_intervals_6h, 0.25),
            "tick_interval_p50_6h": tick_p50_6h,
            "tick_interval_p75_6h": statistics_quantile(tick_intervals_6h, 0.75),
            "tick_interval_p90_6h": statistics_quantile(tick_intervals_6h, 0.90),
            "tick_interval_accel_ratio_2h_vs_6h": accel_ratio_2h_vs_6h,
            "multispike_mode_suggested": multispike_mode_suggested,
            "multispike_buffer_ticks_suggested": multispike_buffer_suggested,
            "multispike_retention_pct_suggested": multispike_retention_suggested,
            "ticks_since_last_spike_now": ticks_since_spike_now,
            "entry_lag_sec_median_6h": statistics_median(lags),
            "entry_lag_sec_p75_6h": statistics_quantile(lags, 0.75),
            "late_entry_ge120_pct_6h": pct(sum(1 for x in lags if x >= 120), len(lags)),
            "zero_peak_exit_count_6h": exits.get("zero_peak_exit", 0),
            "next_spike_after_close_le80_pct_6h": pct(sum(1 for x in early_exit_next_spike if 0 < x <= 80), len(early_exit_next_spike)),
            "spike_rate_prev10m": rate_prev10,
            "spike_rate_last5m": rate_last5,
            "market_regime_estimate": regime,
            "symbol_guard_threshold": SYMBOL_NEGATIVE_THRESHOLD,
            "symbol_guard_pnl_window": guard_pnl_window,
            "symbol_guard_bonus": guard_bonus,
            "symbol_guard_recent_n": guard_recent_n,
            "symbol_guard_active": bool(guard_bonus > 0.0 or guard_pnl_window <= SYMBOL_NEGATIVE_THRESHOLD),
            # Raw spike timestamps used by the market-phase layer (kept under
            # an underscore-prefixed key so the LLM prompt strips them — they
            # are noisy and would bloat the request without adding signal).
            "_spike_ts_recent": [
                t for t, _ in arr
                if t >= (now_ts - MP.RECENT_WINDOW_SEC)
            ],
            "as_of": _now_iso(),
        }
    return telemetry


def statistics_median(values: list[float]) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    n = len(vals)
    mid = n // 2
    if n % 2:
        return float(vals[mid])
    return float((vals[mid - 1] + vals[mid]) / 2.0)


def statistics_quantile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    i = (len(vals) - 1) * p
    lo = int(i)
    hi = min(lo + 1, len(vals) - 1)
    if lo == hi:
        return float(vals[lo])
    frac = i - lo
    return float(vals[lo] * (1 - frac) + vals[hi] * frac)


def pct(a: int, b: int) -> float:
    return (a / b * 100.0) if b else 0.0


def _heuristic_from_telemetry(current_cfg: dict[str, SymbolCfg], telemetry: dict[str, Any]) -> dict[str, SymbolCfg]:
    out: dict[str, SymbolCfg] = {}
    for sym in SYMBOLS:
        base = current_cfg[sym]
        t = telemetry.get(sym, {})
        regime = str(t.get("market_regime_estimate") or base.regime)
        base_floor = _symbol_score_floor(sym)

        spf = base.spike_pre_filter_target
        grace = base.zero_peak_grace_sec
        score = base.score_min_override

        lag_med = t.get("entry_lag_sec_median_6h")
        if lag_med is None:
            lag_med = t.get("entry_lag_sec_median")
        late120 = float(t.get("late_entry_ge120_pct_6h") or t.get("late_entry_ge120_pct") or 0.0)
        early80 = float(
            t.get("next_spike_after_close_le80_pct_6h")
            or t.get("next_spike_after_close_le80_pct")
            or 0.0
        )
        zero_peak_count = int(t.get("zero_peak_exit_count_6h") or t.get("zero_peak_exit_count") or 0)
        win_rate = float(t.get("win_rate_6h") or t.get("win_rate_15m") or 0.0)
        ev_per_trade = float(t.get("ev_per_trade_6h") or t.get("ev_per_trade_15m") or 0.0)
        ai_approval_rate = float(t.get("ai_approval_rate_6h") or t.get("ai_approval_rate_15m") or 0.0)
        contracts_n = int(t.get("contracts_6h") or t.get("contracts_15m") or 0)
        ai_n = int(t.get("ai_decisions_6h") or t.get("ai_decisions_15m") or 0)
        hour_bucket = str(t.get("current_hour_bucket") or "UNKNOWN").upper()
        time_bucket_stats = t.get("time_bucket_stats") if isinstance(t.get("time_bucket_stats"), dict) else {}
        regime_bucket_stats = t.get("regime_bucket_stats") if isinstance(t.get("regime_bucket_stats"), dict) else {}

        risk_points = 0
        recovery_points = 0

        # Entry is tick-driven; when lag is high we must be MORE selective,
        # not less selective, to avoid chase entries.
        if (lag_med is not None and lag_med > 120) or late120 >= 35.0:
            risk_points += 1
            score = score + 0.7
        elif regime == "FAST":
            score = score + 0.4
        elif regime == "SLOW":
            score = score + 0.5

        # Core objective: prevent over-approval when realized edge is negative.
        if contracts_n >= 4 and win_rate < 40.0 and ev_per_trade < 0.0:
            risk_points += 1
            score = score + 0.6
        if contracts_n >= 6 and win_rate < 30.0 and ev_per_trade < -0.05:
            risk_points += 1
            score = score + 0.5
        if ai_n >= 8 and ai_approval_rate >= 85.0 and contracts_n >= 4 and win_rate < 45.0:
            risk_points += 1
            score = score + 0.5
        if ai_n >= 10 and ai_approval_rate >= 92.0 and contracts_n >= 6 and win_rate < 35.0:
            risk_points += 1
            score = score + 0.4

        # Symbol-agnostic quarantine: any symbol can enter strict mode when quality degrades.
        quarantine_floor = base_floor
        if contracts_n >= 4 and late120 >= 25.0:
            risk_points += 1
        if contracts_n >= 4 and zero_peak_count >= 3:
            risk_points += 1
        if contracts_n >= 4 and risk_points >= 2:
            quarantine_floor = min(SCORE_MAX_GUARDRAIL, base_floor + 0.30 * float(risk_points - 1))
            score = score + 0.35 * float(risk_points)
        if contracts_n >= 6 and risk_points >= 4 and ev_per_trade < -0.05:
            score = score + 0.40
            regime = "SLOW"

        # Session-aware calibration: NIGHT/MORNING/ACTIVE/SLOW have different edge quality.
        bucket_row = time_bucket_stats.get(hour_bucket) if isinstance(time_bucket_stats.get(hour_bucket), dict) else {}
        bucket_n = int(bucket_row.get("n") or 0)
        bucket_wr = float(bucket_row.get("win_rate") or 0.0)
        bucket_ev = float(bucket_row.get("ev") or 0.0)
        if bucket_n >= 3 and (bucket_ev < -0.02 or bucket_wr < 35.0):
            risk_points += 1
            score = score + 0.35
        elif bucket_n >= 4 and bucket_ev > 0.03 and bucket_wr >= 55.0:
            recovery_points += 1
            score = score - 0.20

        regime_key = str(regime or "NORMAL").upper()
        regime_row = regime_bucket_stats.get(regime_key) if isinstance(regime_bucket_stats.get(regime_key), dict) else {}
        regime_n = int(regime_row.get("n") or 0)
        regime_ev = float(regime_row.get("ev") or 0.0)
        if regime_n >= 3 and regime_ev < -0.02:
            score = score + 0.25
        elif regime_n >= 4 and regime_ev > 0.02:
            score = score - 0.10

        # Recovery mode: when symbol improves, gradually relax strictness.
        if contracts_n >= 6 and win_rate >= 58.0:
            recovery_points += 1
        if contracts_n >= 6 and ev_per_trade >= 0.03:
            recovery_points += 1
        if contracts_n >= 6 and late120 <= 15.0 and (lag_med is None or lag_med < 100):
            recovery_points += 1
        if ai_n >= 6 and ai_approval_rate <= 75.0:
            recovery_points += 1
        if recovery_points >= 2 and risk_points <= 1:
            score = score - 0.45
        if recovery_points >= 3 and risk_points == 0:
            score = score - 0.25

        # Per-symbol guardrail coming from runtime risk state.
        guard_pnl_window = float(t.get("symbol_guard_pnl_window") or 0.0)
        guard_bonus = max(0.0, float(t.get("symbol_guard_bonus") or 0.0))
        guard_active = bool(
            t.get("symbol_guard_active")
            or guard_bonus > 0.0
            or guard_pnl_window <= float(t.get("symbol_guard_threshold") or SYMBOL_NEGATIVE_THRESHOLD)
        )
        if guard_active:
            risk_points += 1
            # Baseline in bleed mode: 7.5. If bonus has escalated above first
            # step, push floor proportionally (e.g. 1.0 -> 8.0, 1.5 -> 8.5).
            guard_floor = 7.5 + max(0.0, guard_bonus - 0.5)
            score = max(score, guard_floor)
            if guard_bonus >= 1.0 and regime == "FAST":
                regime = "NORMAL"
            if guard_bonus >= 1.5:
                regime = "SLOW"

        score = max(score, quarantine_floor)

        in_quarantine = bool(contracts_n >= 4 and risk_points >= 2)
        prev_quarantine = QUARANTINE_MEMORY.get(sym)
        if prev_quarantine is None or prev_quarantine != in_quarantine:
            if in_quarantine:
                LOG.info(
                    "[dynamic-ai][QUARANTINE_ENTER] %s risk=%d wr=%.1f ev=%.3f late120=%.1f floor=%.2f",
                    sym,
                    risk_points,
                    win_rate,
                    ev_per_trade,
                    late120,
                    quarantine_floor,
                )
            else:
                LOG.info(
                    "[dynamic-ai][QUARANTINE_EXIT] %s recovery=%d wr=%.1f ev=%.3f late120=%.1f",
                    sym,
                    recovery_points,
                    win_rate,
                    ev_per_trade,
                    late120,
                )
        QUARANTINE_MEMORY[sym] = in_quarantine

        if early80 >= 20.0:
            grace = grace + 50
        if early80 >= 35.0 or zero_peak_count >= 3:
            grace = grace + 30

        out[sym] = _clamp_cfg(sym, SymbolCfg(
            regime=regime,
            spike_pre_filter_target=spf,
            zero_peak_grace_sec=grace,
            score_min_override=score,
            is_active=True,
        ))
    return out


def _apply_tick_window_policy(
    current_cfg: dict[str, SymbolCfg],
    candidate_cfg: dict[str, SymbolCfg],
    telemetry: dict[str, Any],
) -> tuple[dict[str, SymbolCfg], int]:
    out: dict[str, SymbolCfg] = {}
    updates = 0
    for sym in SYMBOLS:
        current = current_cfg[sym]
        candidate = candidate_cfg[sym]
        t = telemetry.get(sym, {})

        n_recent = int(t.get("tick_intervals_2h") or 0)
        n_baseline = int(t.get("tick_intervals_6h") or 0)
        p50_recent = t.get("tick_interval_p50_2h")
        mean_recent = t.get("tick_interval_mean_2h")
        p50_baseline = t.get("tick_interval_p50_6h")
        ratio = t.get("tick_interval_accel_ratio_2h_vs_6h")
        if not isinstance(ratio, (int, float)) and isinstance(p50_recent, (int, float)) and isinstance(p50_baseline, (int, float)) and float(p50_baseline) > 0:
            ratio = float(p50_recent) / float(p50_baseline)

        current_target = int(current.spike_pre_filter_target)
        target = current_target
        target_branch = "hold"
        target_raw: float | None = None

        if (
            isinstance(ratio, (int, float))
            and n_recent >= TICK_WINDOW_MIN_SAMPLES
            and n_baseline >= TICK_WINDOW_MIN_SAMPLES
        ):
            if float(ratio) > ACCEL_RATIO_SLOW_THRESHOLD:
                if isinstance(p50_recent, (int, float)):
                    target_raw = float(p50_recent)
                elif isinstance(mean_recent, (int, float)):
                    target_raw = float(mean_recent)
                if target_raw is not None and isinstance(mean_recent, (int, float)) and TICK_TARGET_BLEND_MEAN > 0:
                    target_raw = (
                        target_raw * (1.0 - TICK_TARGET_BLEND_MEAN)
                        + float(mean_recent) * TICK_TARGET_BLEND_MEAN
                    )
                if target_raw is not None:
                    target_raw *= ACCEL_SLOW_TARGET_MULT
                    target_branch = "slow"
            elif float(ratio) < ACCEL_RATIO_FAST_THRESHOLD:
                if isinstance(p50_recent, (int, float)):
                    target_raw = float(p50_recent)
                elif isinstance(mean_recent, (int, float)):
                    target_raw = float(mean_recent)
                if target_raw is not None and isinstance(mean_recent, (int, float)) and TICK_TARGET_BLEND_MEAN > 0:
                    target_raw = (
                        target_raw * (1.0 - TICK_TARGET_BLEND_MEAN)
                        + float(mean_recent) * TICK_TARGET_BLEND_MEAN
                    )
                if target_raw is not None:
                    target_raw *= ACCEL_FAST_TARGET_MULT
                    target_branch = "fast"
            else:
                target_branch = "stable"

        if target_raw is not None:
            target = int(round(target_raw))
            target = max(SPIKE_PREFILTER_MIN_TICKS, min(target, SPIKE_PREFILTER_MAX_TICKS))
            delta = target - current_target
            if abs(delta) > SPIKE_PREFILTER_MAX_STEP_TICKS:
                target = current_target + (SPIKE_PREFILTER_MAX_STEP_TICKS if delta > 0 else -SPIKE_PREFILTER_MAX_STEP_TICKS)

        # Safety cap: never allow prefilter target to consume most of the
        # spike cycle; otherwise symbols can starve (no viable entry window).
        cycle_ticks = _symbol_cycle_ticks(sym)
        if cycle_ticks and cycle_ticks > 0:
            cycle_cap = max(
                SPIKE_PREFILTER_MIN_TICKS,
                min(int(round(float(cycle_ticks) * SPIKE_PREFILTER_MAX_CYCLE_RATIO)), SPIKE_PREFILTER_MAX_TICKS),
            )
            target = min(target, cycle_cap)

        wr_recent = float(t.get("win_rate_2h") or 0.0)
        ev_recent = float(t.get("ev_per_trade_2h") or 0.0)
        score = float(candidate.score_min_override)
        regime = candidate.regime

        if target_branch == "slow":
            regime = "SLOW"
            slow_bonus = ACCEL_SLOW_SCORE_BONUS if (wr_recent < 50.0 or ev_recent < 0.0) else 0.0
            score = max(score, float(current.score_min_override), float(candidate.score_min_override) + slow_bonus)
        elif target_branch == "fast":
            regime = "FAST"
            if wr_recent > 50.0:
                score = max(_symbol_score_floor(sym), score - ACCEL_FAST_SCORE_RELAX)
        elif target_branch == "stable":
            regime = "NORMAL"
            recovery_floor = max(_symbol_score_floor(sym), SCORE_RECOVERY_BASE)
            if score > recovery_floor:
                step = min(SCORE_RECOVERY_STEP_STABLE, score - recovery_floor)
                score = max(recovery_floor, score - step)

        guard_pnl_window = float(t.get("symbol_guard_pnl_window") or 0.0)
        guard_bonus = max(0.0, float(t.get("symbol_guard_bonus") or 0.0))
        guard_active = bool(
            t.get("symbol_guard_active")
            or guard_bonus > 0.0
            or guard_pnl_window <= float(t.get("symbol_guard_threshold") or SYMBOL_NEGATIVE_THRESHOLD)
        )
        if guard_active:
            guard_floor = 7.5 + max(0.0, guard_bonus - 0.5)
            score = max(score, guard_floor)
            if guard_bonus >= 1.5:
                regime = "SLOW"

        adjusted = _clamp_cfg(
            sym,
            SymbolCfg(
                regime=regime,
                spike_pre_filter_target=target,
                zero_peak_grace_sec=candidate.zero_peak_grace_sec,
                score_min_override=score,
                is_active=candidate.is_active,
            ),
        )
        out[sym] = adjusted
        if int(adjusted.spike_pre_filter_target) != int(current_target):
            updates += 1

    return out, updates


def _apply_activity_policy(
    current_cfg: dict[str, SymbolCfg],
    candidate_cfg: dict[str, SymbolCfg],
    telemetry: dict[str, Any],
) -> dict[str, SymbolCfg]:
    out: dict[str, SymbolCfg] = {}
    for sym in SYMBOLS:
        current = current_cfg[sym]
        candidate = candidate_cfg[sym]
        t = telemetry.get(sym, {})

        contracts_n = int(t.get("contracts_15m") or 0)
        win_rate = float(t.get("win_rate_15m") or 0.0)
        ev_per_trade = float(t.get("ev_per_trade_15m") or 0.0)
        late120 = float(t.get("late_entry_ge120_pct") or 0.0)
        lag_med = t.get("entry_lag_sec_median")
        lag_med_val = float(lag_med) if isinstance(lag_med, (int, float)) else None

        severe_disable = False
        recovery_enable = False

        if contracts_n >= 4:
            severe_disable = (
                (win_rate <= 25.0 and ev_per_trade < 0.0)
                or (contracts_n >= 6 and win_rate < 35.0 and ev_per_trade <= -0.05)
                or (late120 >= 45.0 and lag_med_val is not None and lag_med_val >= 180.0)
            )
        if sym in STRICT_DISABLE_SYMBOLS and contracts_n >= STRICT_DISABLE_MIN_CONTRACTS:
            severe_disable = severe_disable or (win_rate < 45.0 or ev_per_trade < 0.0)

        if contracts_n >= 6:
            recovery_enable = (
                win_rate >= 58.0
                and ev_per_trade >= 0.03
                and late120 <= 15.0
                and (lag_med_val is None or lag_med_val < 100.0)
            )

        state = ACTIVITY_POLICY_MEMORY.setdefault(sym, _default_activity_state())
        streak_before = int(state.get("recovery_streak") or 0)
        stabilization_before = int(state.get("stabilization_left") or 0)
        strict_bonus = max(0.0, float(state.get("strict_bonus") or 0.0))

        next_active = current.is_active
        if severe_disable:
            next_active = False
            if streak_before > 0:
                LOG.info(
                    "[dynamic-ai][RECOVERY_RESET] %s streak %d -> 0 (severe_disable)",
                    sym,
                    streak_before,
                )
            state["recovery_streak"] = 0
            state["stabilization_left"] = 0
            state["strict_bonus"] = 0.0
        elif current.is_active:
            state["recovery_streak"] = 0
            if stabilization_before > 0:
                if recovery_enable:
                    state["stabilization_left"] = stabilization_before - 1
                    state["strict_bonus"] = max(0.0, strict_bonus - ACTIVITY_STABILIZATION_RELAX_STEP)
                else:
                    state["stabilization_left"] = stabilization_before
                    state["strict_bonus"] = strict_bonus
            else:
                state["stabilization_left"] = 0
                state["strict_bonus"] = 0.0
        else:
            if recovery_enable:
                next_streak = streak_before + 1
                state["recovery_streak"] = next_streak
                if next_streak == 1 or next_streak == ACTIVITY_RECOVERY_CYCLES:
                    LOG.info(
                        "[dynamic-ai][RECOVERY_PROGRESS] %s streak=%d/%d | contracts=%d wr=%.1f ev=%.3f late120=%.1f lag_med=%s",
                        sym,
                        next_streak,
                        ACTIVITY_RECOVERY_CYCLES,
                        contracts_n,
                        win_rate,
                        ev_per_trade,
                        late120,
                        f"{lag_med_val:.1f}" if lag_med_val is not None else "n/a",
                    )
            else:
                if streak_before > 0:
                    LOG.info(
                        "[dynamic-ai][RECOVERY_RESET] %s streak %d -> 0 (recovery conditions lost)",
                        sym,
                        streak_before,
                    )
                state["recovery_streak"] = 0

            if int(state.get("recovery_streak") or 0) >= ACTIVITY_RECOVERY_CYCLES:
                next_active = True
                state["recovery_streak"] = 0
                state["stabilization_left"] = ACTIVITY_STABILIZATION_CYCLES
                state["strict_bonus"] = ACTIVITY_STABILIZATION_SCORE_BONUS
                LOG.info(
                    "[dynamic-ai][ACTIVITY_RECOVERY] %s re-enabled after %d cycles | stabilize_cycles=%d strict_bonus=%.2f",
                    sym,
                    ACTIVITY_RECOVERY_CYCLES,
                    ACTIVITY_STABILIZATION_CYCLES,
                    ACTIVITY_STABILIZATION_SCORE_BONUS,
                )

        if next_active != current.is_active:
            LOG.info(
                "[dynamic-ai][ACTIVITY_POLICY] %s is_active %s -> %s | contracts=%d wr=%.1f ev=%.3f late120=%.1f lag_med=%s",
                sym,
                current.is_active,
                next_active,
                contracts_n,
                win_rate,
                ev_per_trade,
                late120,
                f"{lag_med_val:.1f}" if lag_med_val is not None else "n/a",
            )

        final_regime = candidate.regime
        final_grace = candidate.zero_peak_grace_sec
        final_score = candidate.score_min_override
        stabilization_left_now = int(state.get("stabilization_left") or 0)
        strict_bonus_now = max(0.0, float(state.get("strict_bonus") or 0.0))

        # Post-reactivation hard mode: keep stricter thresholds while symbol proves stability.
        if next_active and stabilization_left_now > 0:
            final_regime = "SLOW"
            final_grace = max(final_grace, ACTIVITY_STABILIZATION_GRACE_FLOOR)
            strict_floor = min(
                SCORE_MAX_GUARDRAIL,
                _symbol_score_floor(sym) + strict_bonus_now,
            )
            final_score = max(final_score, strict_floor)

        guard_pnl_window = float(t.get("symbol_guard_pnl_window") or 0.0)
        guard_bonus = max(0.0, float(t.get("symbol_guard_bonus") or 0.0))
        guard_active = bool(
            t.get("symbol_guard_active")
            or guard_bonus > 0.0
            or guard_pnl_window <= float(t.get("symbol_guard_threshold") or SYMBOL_NEGATIVE_THRESHOLD)
        )
        if guard_active:
            guard_floor = 7.5 + max(0.0, guard_bonus - 0.5)
            final_score = max(final_score, guard_floor)
            if guard_bonus >= 1.0:
                final_regime = "SLOW"

        out[sym] = _clamp_cfg(
            sym,
            SymbolCfg(
                regime=final_regime,
                spike_pre_filter_target=candidate.spike_pre_filter_target,
                zero_peak_grace_sec=final_grace,
                score_min_override=final_score,
                is_active=next_active,
            ),
        )
    return out


def _apply_market_phase_policy(
    current_cfg: dict[str, SymbolCfg],
    candidate_cfg: dict[str, SymbolCfg],
    telemetry: dict[str, Any],
    baseline: "MP.SessionBaseline | None",
) -> tuple[dict[str, SymbolCfg], dict[str, dict[str, Any]]]:
    """Apply the per-symbol session-phase guard *after* every other policy.

    Returns ``(adjusted_cfg, phase_report)`` where ``phase_report`` maps each
    symbol to the public market-phase dict (also injected into
    ``telemetry[sym]["market_phase"]`` for downstream observability).
    """
    if not MARKET_PHASE_ENABLED or baseline is None:
        return candidate_cfg, {}

    report: dict[str, dict[str, Any]] = {}
    out: dict[str, SymbolCfg] = {}
    for sym in SYMBOLS:
        candidate = candidate_cfg[sym]
        t = telemetry.get(sym, {}) or {}
        spike_ts = t.get("_spike_ts_recent") or []
        try:
            phase = MP.evaluate_symbol(sym, spike_ts, baseline)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("[market-phase] %s evaluation failed: %s", sym, exc)
            out[sym] = candidate
            continue

        phase_payload = MP.phase_to_dict(phase)
        # Make the phase visible to the dashboard / diff log.
        if isinstance(telemetry.get(sym), dict):
            telemetry[sym]["market_phase"] = phase_payload
        report[sym] = phase_payload

        # NORMAL / warm-up: no adjustment.
        if phase.phase == MP.PHASE_NORMAL:
            out[sym] = candidate
            continue

        regime = candidate.regime
        score = float(candidate.score_min_override)
        grace = int(candidate.zero_peak_grace_sec)
        is_active = bool(candidate.is_active)

        if phase.phase == MP.PHASE_ACCEL:
            # Symbol is hotter than its own historical norm: relax score a hair
            # but never drop below per-symbol floor. Keep regime as-is.
            score = max(_symbol_score_floor(sym), score - MARKET_PHASE_ACCEL_SCORE_RELAX)

        elif phase.phase == MP.PHASE_CAUTION:
            # Mild deceleration: harden score and prefer NORMAL/SLOW regime.
            score = score + MARKET_PHASE_CAUTION_SCORE_BUMP
            if regime == "FAST":
                regime = "NORMAL"

        elif phase.phase == MP.PHASE_DECEL:
            # Strong deceleration: force SLOW + tighter grace + score bump.
            score = score + MARKET_PHASE_DECEL_SCORE_BUMP
            regime = "SLOW"
            grace = min(120, grace + MARKET_PHASE_DECEL_GRACE_BUMP)

        elif phase.phase == MP.PHASE_DEAD:
            # The symbol is essentially silent vs its baseline. Force SLOW,
            # max grace, and optionally deactivate (it will be re-enabled by
            # the activity policy once it recovers).
            score = score + MARKET_PHASE_DEAD_SCORE_BUMP
            regime = "SLOW"
            grace = min(120, grace + MARKET_PHASE_DECEL_GRACE_BUMP)
            if MARKET_PHASE_DEAD_DISABLES:
                is_active = False

        out[sym] = _clamp_cfg(
            sym,
            SymbolCfg(
                regime=regime,
                spike_pre_filter_target=candidate.spike_pre_filter_target,
                zero_peak_grace_sec=grace,
                score_min_override=score,
                is_active=is_active,
            ),
        )

        prev = current_cfg.get(sym)
        if prev is None or out[sym] != prev:
            LOG.info(
                "[market-phase] %s phase=%s ratio=%s recent=%.3f base=%s samples=%d streak=%d -> regime=%s score=%.2f grace=%d active=%s",
                sym,
                phase.phase,
                f"{phase.ratio:.2f}" if isinstance(phase.ratio, (int, float)) else "n/a",
                phase.recent_rate,
                f"{phase.baseline_rate:.3f}" if isinstance(phase.baseline_rate, (int, float)) else "n/a",
                phase.samples_baseline,
                phase.streak,
                out[sym].regime,
                out[sym].score_min_override,
                out[sym].zero_peak_grace_sec,
                out[sym].is_active,
            )

    return out, report


def _update_market_phase_baseline(
    telemetry: dict[str, Any],
    baseline: "MP.SessionBaseline | None",
) -> None:
    """After deciding the new config, feed the current recent rate back into
    the per-(symbol, hour) EMA so the baseline drifts slowly with reality.
    """
    if not MARKET_PHASE_ENABLED or baseline is None:
        return
    now_ts = time.time()
    hour = MP.hour_bucket_of(now_ts)
    for sym in SYMBOLS:
        t = telemetry.get(sym, {}) or {}
        spike_ts = t.get("_spike_ts_recent") or []
        rate, _n = MP.spikes_per_min(spike_ts, MP.RECENT_WINDOW_SEC, now_ts)
        try:
            baseline.update(sym, hour, rate, now_ts)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("[market-phase] %s baseline update failed: %s", sym, exc)
    try:
        baseline.save()
    except Exception as exc:  # noqa: BLE001
        LOG.warning("[market-phase] baseline save failed: %s", exc)


def _scrub_telemetry_for_prompt(telemetry: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of telemetry suitable for the LLM prompt.

    We drop the underscore-prefixed internals (raw timestamp lists, etc.) to
    keep the request small and the model focused on aggregated features.
    """
    scrubbed: dict[str, Any] = {}
    for sym, payload in telemetry.items():
        if not isinstance(payload, dict):
            scrubbed[sym] = payload
            continue
        scrubbed[sym] = {k: v for k, v in payload.items() if not str(k).startswith("_")}
    return scrubbed


def _build_prompt(telemetry_json: dict[str, Any]) -> str:
    score_max = f"{SCORE_MAX_GUARDRAIL:.1f}"
    return (
        "Actua como un motor cuantitativo de alta frecuencia para indices sinteticos Deriv.\n"
        "Tu objetivo es ajustar los parametros de trading en tiempo real para evitar entradas tardias y salidas prematuras.\n\n"
        "DATOS DE TELEMETRIA (micro 15m + macro 6h por simbolo):\n"
        f"{json.dumps(telemetry_json, ensure_ascii=True)}\n\n"
        "REGLAS DE AJUSTE:\n"
        "1. ENTRADAS SON TICK-DRIVEN: NO usar eventos de spike para decidir entrada directa.\n"
        "2. No modificar spike_pre_filter_target automaticamente; el sidecar lo recalcula fuera del LLM con ventana reactiva 2h y baseline 6h.\n"
        "3. Si entry_lag_sec >120s o mercado FAST, SUBE score_min_override para endurecer entradas y evitar chase.\n"
        "4. Si hay zero_peak_exit antes del spike siguiente (<80s), aumentar zero_peak_grace_sec para esperar mas.\n"
        "5. Si mercado SLOW, subir score_min_override para evitar ruido.\n"
        "6. Regla de Recuperacion (Regimen Estable): Si 0.8 <= Ratio <= 1.2 (el mercado esta a un ritmo normal y estable), tu obligacion es RELAJAR la cuarentena. Debes bajar gradualmente el score_min_override acercandolo de nuevo al valor base de 6.80 (ejemplo: si estaba en 8.00, bajalo a 7.20; si estaba en 7.20, bajalo a 6.80). No mantengas los scores en 8.00 si el ratio de aceleracion es normal, o asfixiaras la estrategia.\n"
        "7. Si ai_approval_rate_6h >85% y win_rate_6h <45% (o ev_per_trade_6h <0), ENDURECER score_min_override inmediatamente.\n"
        "8. Si symbol_guard_active=true (o symbol_guard_pnl_window <= -2.0), imponer piso dinamico estricto: score_min_override >= 7.5 + max(0, symbol_guard_bonus-0.5).\n"
        "9. Si symbol_guard_bonus >= 1.0, preferir regime=SLOW para ese simbolo hasta que mejore.\n"
        f"10. Mantener guardrails: score_min_override [6.5,{score_max}], zero_peak_grace_sec [0,120].\n"
        "11. BOOM500/CRASH500/CRASH600 deben mantener zero_peak_grace_sec >= 60.\n"
        "12. Cualquier simbolo puede entrar en cuarentena soft si cae su calidad (WR bajo, EV negativo, lag alto).\n"
        "13. Cuarentena soft NO deshabilita: solo endurece score_min_override para reducir entradas de baja calidad.\n"
        "14. Si un simbolo se recupera (WR/EV/lag mejoran), relajar score_min_override gradualmente.\n"
        "15. Evitar cambiar de regime continuamente; priorizar estabilidad macro.\n"
        "16. Cada simbolo trae un objeto market_phase con: phase (ACCEL/NORMAL/CAUTION/DECEL/DEAD), ratio (recent/baseline spikes-per-min para la hora UTC actual), baseline_rate y samples_baseline. Es una capa estructural ABOVE intra-session signals: si phase=DECEL o DEAD, ese simbolo esta produciendo MENOS spikes de los que historicamente produce en esta franja horaria — endurecer fuerte (regime=SLOW, score_min_override >= 7.4). Si phase=CAUTION, endurecer leve (+0.4). Si phase=ACCEL, NO relajar mas alla del piso del simbolo. Si market_phase no esta presente o samples_baseline < 5, ignorar.\n"
        "17. Cuando phase in {DECEL, DEAD} y el LLM responde, NUNCA bajar score_min_override por debajo del valor actual del simbolo — el sidecar aplicara su propio piso despues.\n\n"
        "Devuelve UNICAMENTE un JSON valido sin markdown ni texto extra con esta forma:\n"
        "{\n"
        "  \"BOOM1000\": {\"regime\": \"FAST\", \"spike_pre_filter_target\": 120, \"zero_peak_grace_sec\": 60, \"score_min_override\": 6.8}\n"
        "}\n"
    )


def _call_llm(prompt: str) -> dict[str, Any]:
    api_key = (
        os.getenv("DYNAMIC_AI_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
        or ""
    ).strip()
    if not api_key:
        raise RuntimeError("missing API key (DYNAMIC_AI_API_KEY/OPENAI_API_KEY/OPENROUTER_API_KEY)")

    api_url = os.getenv("DYNAMIC_AI_BASE_URL", "https://api.openai.com/v1/chat/completions").strip()
    model = os.getenv("DYNAMIC_AI_MODEL", "gpt-4o-mini").strip()
    timeout_s = max(10, int(os.getenv("DYNAMIC_AI_TIMEOUT_SEC", "40") or 40))

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # OpenRouter compatibility
    if "openrouter.ai" in api_url:
        headers.setdefault("HTTP-Referer", os.getenv("DYNAMIC_AI_REFERER", "https://localhost"))
        headers.setdefault("X-Title", os.getenv("DYNAMIC_AI_TITLE", "Deriv Dynamic Orchestrator"))

    payload = {
        "model": model,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "You are a strict JSON-only quantitative controller."},
            {"role": "user", "content": prompt},
        ],
    }

    resp = requests.post(api_url, headers=headers, json=payload, timeout=timeout_s)
    resp.raise_for_status()
    body = resp.json()
    content = body["choices"][0]["message"]["content"]
    return _parse_json_maybe_fenced(content)


async def _read_current_cfg(conn: Any) -> dict[str, SymbolCfg]:
    rows = await conn.fetch(
        """
        SELECT symbol, market_regime, spike_pre_filter_target,
               zero_peak_grace_sec, score_min_override, is_active
        FROM dynamic_symbol_config
        """
    )
    out: dict[str, SymbolCfg] = {}
    for row in rows:
        sym = str(row["symbol"] or "").upper()
        if not sym:
            continue
        out[sym] = _clamp_cfg(sym, SymbolCfg(
            regime=str(row["market_regime"] or "NORMAL").upper(),
            spike_pre_filter_target=int(row["spike_pre_filter_target"] or 280),
            zero_peak_grace_sec=int(row["zero_peak_grace_sec"] or 0),
            score_min_override=float(row["score_min_override"] or 6.0),
            is_active=bool(row["is_active"]),
        ))

    # Ensure defaults for missing symbols
    for sym in SYMBOLS:
        out.setdefault(sym, _clamp_cfg(sym, SymbolCfg("NORMAL", 280, 0, 7.0, True)))
    return out


def _build_cfg_from_llm(raw: dict[str, Any], current_cfg: dict[str, SymbolCfg]) -> dict[str, SymbolCfg]:
    out: dict[str, SymbolCfg] = {}
    for sym in SYMBOLS:
        cur = current_cfg[sym]
        payload = raw.get(sym) if isinstance(raw.get(sym), dict) else {}
        # spike_pre_filter_target intentionally frozen: entry is tick-driven.
        out[sym] = _clamp_cfg(sym, SymbolCfg(
            regime=str(payload.get("regime", cur.regime)).upper(),
            spike_pre_filter_target=int(cur.spike_pre_filter_target),
            zero_peak_grace_sec=int(payload.get("zero_peak_grace_sec", cur.zero_peak_grace_sec)),
            score_min_override=float(payload.get("score_min_override", cur.score_min_override)),
            is_active=cur.is_active,
        ))
    return out


async def _apply_cfg(
    conn: Any,
    current_cfg: dict[str, SymbolCfg],
    cfgs: dict[str, SymbolCfg],
    decision_source: str,
    diff_log_path: Path,
) -> int:
    updates = 0
    db_score_max = await _detect_db_score_max(conn)
    async with conn.transaction():
        for sym, cfg in cfgs.items():
            previous = current_cfg.get(sym)
            if previous is not None and not _cfg_changed(previous, cfg):
                continue

            cfg_to_write = cfg

            # Compatibility fallback for environments where migration 011
            # (score guardrail upper bound 9.2) has not been applied yet.
            fallback_max = max(
                SCORE_MIN_GUARDRAIL,
                min(db_score_max, SCORE_MAX_DB_COMPAT_FALLBACK, SCORE_MAX_GUARDRAIL),
            )
            fallback_score = min(float(cfg_to_write.score_min_override), fallback_max)
            if fallback_score < float(cfg_to_write.score_min_override):
                LOG.warning(
                    "[dynamic-ai][DB_COMPAT] %s score %.2f -> %.2f due to DB constraint (max=%.2f)",
                    sym,
                    float(cfg_to_write.score_min_override),
                    float(fallback_score),
                    float(fallback_max),
                )
                cfg_to_write = _clamp_cfg(
                    sym,
                    SymbolCfg(
                        regime=cfg_to_write.regime,
                        spike_pre_filter_target=cfg_to_write.spike_pre_filter_target,
                        zero_peak_grace_sec=cfg_to_write.zero_peak_grace_sec,
                        score_min_override=fallback_score,
                        is_active=cfg_to_write.is_active,
                    ),
                )

            await conn.execute(
                """
                INSERT INTO dynamic_symbol_config (
                    symbol, market_regime, spike_pre_filter_target,
                    zero_peak_grace_sec, score_min_override, is_active
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (symbol) DO UPDATE SET
                    market_regime = EXCLUDED.market_regime,
                    spike_pre_filter_target = EXCLUDED.spike_pre_filter_target,
                    zero_peak_grace_sec = EXCLUDED.zero_peak_grace_sec,
                    score_min_override = EXCLUDED.score_min_override,
                    is_active = EXCLUDED.is_active,
                    last_updated = NOW()
                """,
                sym,
                cfg_to_write.regime,
                cfg_to_write.spike_pre_filter_target,
                cfg_to_write.zero_peak_grace_sec,
                cfg_to_write.score_min_override,
                cfg_to_write.is_active,
            )
            updates += 1

            if previous is not None:
                diffs = _cfg_diff_items(previous, cfg_to_write)
            else:
                diffs = [f"created: {_cfg_to_dict(cfg_to_write)}"]
            if diffs:
                LOG.info("[dynamic-ai][DIFF] %s modified | %s", sym, " | ".join(diffs))
                _append_diff_jsonl(
                    diff_log_path,
                    {
                        "ts": _now_iso(),
                        "symbol": sym,
                        "source": decision_source,
                        "changes": diffs,
                        "old": _cfg_to_dict(previous) if previous is not None else None,
                        "new": _cfg_to_dict(cfg_to_write),
                    },
                )
    return updates


async def _detect_db_score_max(conn: Any) -> float:
    default_max = max(
        SCORE_MIN_GUARDRAIL,
        min(SCORE_MAX_DB_COMPAT_FALLBACK, SCORE_MAX_GUARDRAIL),
    )
    try:
        cdef = await conn.fetchval(
            """
            SELECT pg_get_constraintdef(c.oid)
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            WHERE t.relname = 'dynamic_symbol_config'
              AND c.conname = 'chk_dsc_score_min_override'
            """
        )
        text = str(cdef or "")
        match = re.search(r"<=\s*\(([-+]?[0-9]*\.?[0-9]+)\)::double precision", text)
        if not match:
            match = re.search(r"BETWEEN\s+[-+]?[0-9]*\.?[0-9]+\s+AND\s+([-+]?[0-9]*\.?[0-9]+)", text)
        if match:
            parsed = float(match.group(1))
            if parsed >= SCORE_MIN_GUARDRAIL:
                return max(SCORE_MIN_GUARDRAIL, min(parsed, SCORE_MAX_GUARDRAIL))
    except Exception:
        pass
    return default_max


def _bucket(v: Any, step: float, ndigits: int = 3) -> float:
    try:
        x = float(v)
    except Exception:
        return 0.0
    if step <= 0:
        return round(x, ndigits)
    return round(round(x / step) * step, ndigits)


def _extract_pattern_key(row: dict[str, Any]) -> tuple[str, str, str, str, float, float, float, float]:
    symbol = str(row.get("symbol") or "").upper()
    side = str(row.get("side") or "").upper() or "UNKNOWN"
    sb = row.get("score_breakdown") if isinstance(row.get("score_breakdown"), dict) else {}
    setup = str(sb.get("setup_type") or sb.get("entry_setup") or "unknown").lower()
    regime = str(sb.get("market_regime") or sb.get("regime") or "normal").upper()
    score_raw = row.get("score")
    if score_raw is None:
        score_raw = sb.get("score_raw")
    hurst_raw = row.get("hurst")
    if hurst_raw is None:
        hurst_raw = sb.get("hurst")
    atr_raw = sb.get("atr_pct_at_entry")
    if atr_raw is None:
        atr_raw = sb.get("atr_pct")
    geo_raw = sb.get("geo_channel_pos")
    return (
        symbol,
        side,
        setup,
        regime,
        _bucket(score_raw, 0.25, 2),
        _bucket(hurst_raw, 0.02, 2),
        _bucket(atr_raw, 0.001, 4),
        _bucket(geo_raw, 0.1, 2),
    )


async def _update_pattern_memory(conn: Any, logs_dir: Path) -> None:
    if not PATTERN_MEMORY_ENABLED:
        return

    closed = _load_json(logs_dir / "deriv_closed_contracts.json")
    if not closed:
        return

    rows = closed[-PATTERN_MEMORY_LOOKBACK:]
    agg: dict[tuple[str, str, str, str, float, float, float, float], dict[str, Any]] = {}
    for row in rows:
        key = _extract_pattern_key(row)
        symbol = key[0]
        if not symbol:
            continue
        slot = agg.setdefault(
            key,
            {
                "sample_trades": 0,
                "wins": 0,
                "losses": 0,
                "pnl_sum": 0.0,
                "last_trade_ts": None,
            },
        )
        try:
            pnl = float(row.get("realized_pnl_usdt") or 0.0)
        except Exception:
            pnl = 0.0
        slot["sample_trades"] += 1
        slot["pnl_sum"] += pnl
        if pnl > 0:
            slot["wins"] += 1
        elif pnl < 0:
            slot["losses"] += 1
        close_ts = _safe_ts(row.get("closed_at_ts") or row.get("closed_at"))
        prev_ts = slot.get("last_trade_ts")
        if close_ts is not None and (prev_ts is None or close_ts > prev_ts):
            slot["last_trade_ts"] = close_ts

    if not agg:
        return

    async with conn.transaction():
        for key, stats in agg.items():
            symbol, side, setup, regime, score_bucket, hurst_bucket, atr_bucket, geo_bucket = key
            trades = int(stats["sample_trades"])
            wins = int(stats["wins"])
            losses = int(stats["losses"])
            pnl_sum = float(stats["pnl_sum"])
            win_rate = (wins / trades * 100.0) if trades else 0.0
            avg_pnl = (pnl_sum / trades) if trades else 0.0
            last_trade_ts = stats.get("last_trade_ts")
            last_trade_dt = (
                datetime.fromtimestamp(last_trade_ts, tz=timezone.utc)
                if isinstance(last_trade_ts, (int, float))
                else None
            )

            await conn.execute(
                """
                INSERT INTO ai_entry_pattern_memory (
                    symbol,
                    side,
                    setup_type,
                    regime,
                    score_bucket,
                    hurst_bucket,
                    atr_bucket,
                    geo_bucket,
                    sample_trades,
                    wins,
                    losses,
                    win_rate,
                    avg_pnl_usdt,
                    last_trade_ts,
                    updated_at
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8,
                    $9, $10, $11, $12, $13, $14, NOW()
                )
                ON CONFLICT (
                    symbol, side, setup_type, regime,
                    score_bucket, hurst_bucket, atr_bucket, geo_bucket
                ) DO UPDATE SET
                    sample_trades = EXCLUDED.sample_trades,
                    wins = EXCLUDED.wins,
                    losses = EXCLUDED.losses,
                    win_rate = EXCLUDED.win_rate,
                    avg_pnl_usdt = EXCLUDED.avg_pnl_usdt,
                    last_trade_ts = EXCLUDED.last_trade_ts,
                    updated_at = NOW()
                """,
                symbol,
                side,
                setup,
                regime,
                score_bucket,
                hurst_bucket,
                atr_bucket,
                geo_bucket,
                trades,
                wins,
                losses,
                win_rate,
                avg_pnl,
                last_trade_dt,
            )


async def run_loop() -> None:
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL is required")

    loop_raw = os.getenv("DYNAMIC_AI_LOOP_SEC") or os.getenv("DYNAMIC_AI_INTERVAL_SEC") or "500"
    loop_sec = max(120, int(loop_raw or 500))
    lookback_sec = TELEMETRY_LOOKBACK_SEC_DEFAULT
    logs_dir = Path(
        os.getenv("DYNAMIC_AI_LOGS_DIR")
        or os.getenv("DYNAMIC_AI_LOG_DIR")
        or os.getenv("LOGS_DIR")
        or "/data/logs"
    ).expanduser()
    diff_log_path = Path(
        os.getenv("DYNAMIC_AI_DIFF_LOG_PATH")
        or str(logs_dir / "dynamic_ai_config_diffs.jsonl")
    ).expanduser()
    activity_state_path = Path(
        os.getenv("DYNAMIC_AI_ACTIVITY_STATE_PATH")
        or str(logs_dir / "dynamic_ai_activity_state.json")
    ).expanduser()
    session_baseline_path = Path(
        os.getenv("MARKET_PHASE_BASELINE_PATH")
        or str(logs_dir / "deriv_session_baseline.json")
    ).expanduser()

    _load_activity_memory(activity_state_path)

    session_baseline: "MP.SessionBaseline | None" = None
    if MARKET_PHASE_ENABLED:
        try:
            session_baseline = MP.SessionBaseline(session_baseline_path)
            LOG.info(
                "[market-phase] baseline loaded path=%s window=%ss alpha=%.2f thresholds(accel=%.2f caution=%.2f decel=%.2f dead=%.2f) confirm(decel=%d dead=%d)",
                session_baseline_path,
                MP.RECENT_WINDOW_SEC,
                MP.EMA_ALPHA,
                MP.ACCEL_RATIO,
                MP.CAUTION_RATIO,
                MP.DECEL_RATIO,
                MP.DEAD_RATIO,
                MP.CONFIRM_CYCLES_DECEL,
                MP.CONFIRM_CYCLES_DEAD,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("[market-phase] disabled (baseline load failed): %s", exc)
            session_baseline = None

    import asyncpg  # noqa: PLC0415

    LOG.info(
        "[dynamic-ai] starting loop interval=%ss logs_dir=%s lookback=%ss strict_disable=%s min_contracts=%d recovery_cycles=%d stabilization_cycles=%d",
        loop_sec,
        logs_dir,
        lookback_sec,
        sorted(STRICT_DISABLE_SYMBOLS),
        STRICT_DISABLE_MIN_CONTRACTS,
        ACTIVITY_RECOVERY_CYCLES,
        ACTIVITY_STABILIZATION_CYCLES,
    )

    while True:
        started = time.time()
        conn = None
        try:
            conn = await asyncpg.connect(dsn, timeout=12.0)
            current_cfg = await _read_current_cfg(conn)
            _seed_state_memory(current_cfg)
            _seed_activity_memory(current_cfg)
            telemetry = _build_telemetry_from_logs(logs_dir, lookback_sec=lookback_sec)
            if PATTERN_MEMORY_ENABLED:
                try:
                    await _update_pattern_memory(conn, logs_dir)
                except Exception as memory_exc:  # noqa: BLE001
                    LOG.warning("[dynamic-ai] pattern memory skipped: %s", memory_exc)
            prompt = _build_prompt(_scrub_telemetry_for_prompt(telemetry))

            try:
                llm_raw = _call_llm(prompt)
                new_cfg = _build_cfg_from_llm(llm_raw, current_cfg)
                decision_source = "llm"
            except Exception as llm_exc:  # noqa: BLE001
                LOG.warning("[dynamic-ai] LLM unavailable (%s) -> heuristic fallback", llm_exc)
                new_cfg = _heuristic_from_telemetry(current_cfg, telemetry)
                decision_source = "heuristic"

            new_cfg = _apply_activity_policy(current_cfg, new_cfg, telemetry)
            new_cfg, tick_updates = _apply_tick_window_policy(current_cfg, new_cfg, telemetry)
            new_cfg, phase_report = _apply_market_phase_policy(
                current_cfg, new_cfg, telemetry, session_baseline
            )

            smoothed_cfg, blocked_regime_flips = _apply_smoothing_all(new_cfg)
            updates = await _apply_cfg(
                conn,
                current_cfg,
                smoothed_cfg,
                decision_source,
                diff_log_path,
            )
            LOG.info(
                "[dynamic-ai] applied config source=%s symbols=%d db_updates=%d blocked_flips=%d sample=%s",
                decision_source,
                len(smoothed_cfg),
                updates,
                blocked_regime_flips,
                {
                    "BOOM500": smoothed_cfg.get("BOOM500").__dict__ if smoothed_cfg.get("BOOM500") else None,
                    "CRASH900": smoothed_cfg.get("CRASH900").__dict__ if smoothed_cfg.get("CRASH900") else None,
                },
            )
            LOG.info(
                "[dynamic-ai][TICK_WINDOW_ACCEL] spike_pre_filter_updates=%d sample=%s",
                tick_updates,
                {
                    "BOOM500": {
                        "ratio": telemetry.get("BOOM500", {}).get("tick_interval_accel_ratio_2h_vs_6h"),
                        "p50_2h": telemetry.get("BOOM500", {}).get("tick_interval_p50_2h"),
                        "p50_6h": telemetry.get("BOOM500", {}).get("tick_interval_p50_6h"),
                    },
                    "CRASH500": {
                        "ratio": telemetry.get("CRASH500", {}).get("tick_interval_accel_ratio_2h_vs_6h"),
                        "p50_2h": telemetry.get("CRASH500", {}).get("tick_interval_p50_2h"),
                        "p50_6h": telemetry.get("CRASH500", {}).get("tick_interval_p50_6h"),
                    },
                    "BOOM900": {
                        "ratio": telemetry.get("BOOM900", {}).get("tick_interval_accel_ratio_2h_vs_6h"),
                        "p50_2h": telemetry.get("BOOM900", {}).get("tick_interval_p50_2h"),
                        "p50_6h": telemetry.get("BOOM900", {}).get("tick_interval_p50_6h"),
                    },
                    "CRASH900": {
                        "ratio": telemetry.get("CRASH900", {}).get("tick_interval_accel_ratio_2h_vs_6h"),
                        "p50_2h": telemetry.get("CRASH900", {}).get("tick_interval_p50_2h"),
                        "p50_6h": telemetry.get("CRASH900", {}).get("tick_interval_p50_6h"),
                    },
                },
            )
            # Always write a heartbeat entry so diff_log mtime stays fresh
            # even when hysteresis blocks all config changes.
            _append_diff_jsonl(
                diff_log_path,
                {
                    "ts": _now_iso(),
                    "type": "heartbeat",
                    "source": decision_source,
                    "db_updates": updates,
                    "blocked_flips": blocked_regime_flips,
                    "tick_window_updates": tick_updates,
                    "market_phase": {
                        sym: {
                            "phase": payload.get("phase"),
                            "ratio": payload.get("ratio"),
                            "samples": payload.get("samples_baseline"),
                            "streak": payload.get("streak"),
                        }
                        for sym, payload in (phase_report or {}).items()
                    },
                },
            )
            _save_activity_memory(activity_state_path)
            _update_market_phase_baseline(telemetry, session_baseline)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("[dynamic-ai] loop failure: %s", exc)
            _save_activity_memory(activity_state_path)
        finally:
            if conn is not None:
                with suppress(Exception):
                    await conn.close()

        elapsed = time.time() - started
        sleep_for = max(1.0, loop_sec - elapsed)
        await asyncio.sleep(sleep_for)

def main() -> None:
    logging.basicConfig(
        level=os.getenv("DYNAMIC_AI_LOG_LEVEL", "INFO"),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    asyncio.run(run_loop())


if __name__ == "__main__":
    main()
