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
    "BOOM500": float(os.getenv("DYNAMIC_AI_BOOM500_SCORE_FLOOR", "8.0") or 8.0),
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
PATTERN_MEMORY_ENABLED = os.getenv("DYNAMIC_AI_PATTERN_MEMORY_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
PATTERN_MEMORY_LOOKBACK = max(
    80,
    int(os.getenv("DYNAMIC_AI_PATTERN_MEMORY_LOOKBACK", "500") or 500),
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
        spike_pre_filter_target=max(50, min(int(cfg.spike_pre_filter_target), 500)),
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
        }
        return new_config, True

    mem = STATE_MEMORY[sym]
    previous_cfg = mem.get("config")
    if not isinstance(previous_cfg, SymbolCfg):
        previous_cfg = _clamp_cfg(sym, SymbolCfg("NORMAL", 280, 0, 7.0, True))

    previous_regime = str(mem.get("regime") or previous_cfg.regime or "NORMAL").upper()
    next_regime = str(new_config.regime or previous_regime).upper()
    time_since_last_change = now_ts - float(mem.get("last_changed") or now_ts)

    if next_regime == previous_regime:
        mem["config"] = new_config
        return new_config, True

    emergency_change = (not bool(new_config.is_active)) or next_regime == "DORMANT"
    if time_since_last_change < MIN_STATE_LIFETIME_SEC and not emergency_change:
        return previous_cfg, False

    STATE_MEMORY[sym] = {
        "regime": next_regime,
        "config": new_config,
        "last_changed": now_ts,
    }
    return new_config, True


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


def _build_telemetry_from_logs(logs_dir: Path, lookback_sec: int = 900) -> dict[str, Any]:
    spikes = _load_json(logs_dir / "deriv_spike_events.json")
    closed = _load_json(logs_dir / "deriv_closed_contracts.json")
    ai_decisions = _load_json(logs_dir / "deriv_ai_decisions.json")

    now_ts = time.time()
    min_ts = now_ts - lookback_sec

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

    telemetry: dict[str, Any] = {}
    for sym in SYMBOLS:
        arr = spikes_by.get(sym, [])
        recent = [(t, s) for t, s in arr if t >= min_ts]
        entered = [s for _, s in recent if bool(s.get("bot_entered"))]
        blocked = [s for _, s in recent if not bool(s.get("bot_entered"))]

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
        early_exit_next_spike: list[float] = []
        close_rows = closes_by.get(sym, [])
        spike_times = [t for t, _ in arr]
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

        exits = Counter(str(c.get("exit_reason") or c.get("close_reason") or "") for c in close_rows)
        block_reasons = Counter(str(s.get("block_reason") or "none") for s in blocked)
        pnl_values: list[float] = []
        wins = 0
        for c in close_rows:
            try:
                pnl = float(c.get("realized_pnl_usdt") or 0.0)
            except Exception:
                pnl = 0.0
            pnl_values.append(pnl)
            if pnl > 0.0:
                wins += 1

        ai_rows = ai_by.get(sym, [])
        ai_n = len(ai_rows)
        ai_approvals = sum(1 for r in ai_rows if bool(r.get("approved")))
        contracts_n = len(close_rows)

        telemetry[sym] = {
            "spikes_15m": len(recent),
            "entered_spikes_15m": len(entered),
            "blocked_spikes_15m": len(blocked),
            "entry_rate_pct": (len(entered) / len(recent) * 100.0) if recent else 0.0,
            "top_block_reasons": block_reasons.most_common(5),
            "contracts_15m": contracts_n,
            "win_rate_15m": pct(wins, contracts_n),
            "ev_per_trade_15m": (sum(pnl_values) / contracts_n) if contracts_n else 0.0,
            "ai_decisions_15m": ai_n,
            "ai_approval_rate_15m": pct(ai_approvals, ai_n),
            "entry_lag_sec_median": statistics_median(lags),
            "entry_lag_sec_p75": statistics_quantile(lags, 0.75),
            "late_entry_ge120_pct": pct(sum(1 for x in lags if x >= 120), len(lags)),
            "zero_peak_exit_count": exits.get("zero_peak_exit", 0),
            "next_spike_after_close_le80_pct": pct(sum(1 for x in early_exit_next_spike if 0 < x <= 80), len(early_exit_next_spike)),
            "spike_rate_prev10m": rate_prev10,
            "spike_rate_last5m": rate_last5,
            "market_regime_estimate": regime,
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

        lag_med = t.get("entry_lag_sec_median")
        late120 = float(t.get("late_entry_ge120_pct") or 0.0)
        early80 = float(t.get("next_spike_after_close_le80_pct") or 0.0)
        zero_peak_count = int(t.get("zero_peak_exit_count") or 0)
        win_rate = float(t.get("win_rate_15m") or 0.0)
        ev_per_trade = float(t.get("ev_per_trade_15m") or 0.0)
        ai_approval_rate = float(t.get("ai_approval_rate_15m") or 0.0)
        contracts_n = int(t.get("contracts_15m") or 0)
        ai_n = int(t.get("ai_decisions_15m") or 0)

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


def _build_prompt(telemetry_json: dict[str, Any]) -> str:
    score_max = f"{SCORE_MAX_GUARDRAIL:.1f}"
    return (
        "Actua como un motor cuantitativo de alta frecuencia para indices sinteticos Deriv.\n"
        "Tu objetivo es ajustar los parametros de trading en tiempo real para evitar entradas tardias y salidas prematuras.\n\n"
        "DATOS DE TELEMETRIA (Ultimos 15 mins):\n"
        f"{json.dumps(telemetry_json, ensure_ascii=True)}\n\n"
        "REGLAS DE AJUSTE:\n"
        "1. ENTRADAS SON TICK-DRIVEN: NO usar eventos de spike para decidir entrada directa.\n"
        "2. No modificar spike_pre_filter_target automaticamente; mantener el valor actual (solo observabilidad).\n"
        "3. Si entry_lag_sec >120s o mercado FAST, SUBE score_min_override para endurecer entradas y evitar chase.\n"
        "4. Si hay zero_peak_exit antes del spike siguiente (<80s), aumentar zero_peak_grace_sec para esperar mas.\n"
        "5. Si mercado SLOW, subir score_min_override para evitar ruido.\n"
        "6. Si ai_approval_rate_15m >85% y win_rate_15m <45% (o ev_per_trade_15m <0), ENDURECER score_min_override inmediatamente.\n"
        f"7. Mantener guardrails: score_min_override [6.5,{score_max}], zero_peak_grace_sec [0,120].\n"
        "8. BOOM500/CRASH500/CRASH600 deben mantener zero_peak_grace_sec >= 60.\n"
        "9. Cualquier simbolo puede entrar en cuarentena soft si cae su calidad (WR bajo, EV negativo, lag alto).\n"
        "10. Cuarentena soft NO deshabilita: solo endurece score_min_override para reducir entradas de baja calidad.\n"
        "11. Si un simbolo se recupera (WR/EV/lag mejoran), relajar score_min_override gradualmente.\n"
        "12. Evitar cambiar de regime continuamente; priorizar estabilidad macro.\n\n"
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

    _load_activity_memory(activity_state_path)

    import asyncpg  # noqa: PLC0415

    LOG.info(
        "[dynamic-ai] starting loop interval=%ss logs_dir=%s strict_disable=%s min_contracts=%d recovery_cycles=%d stabilization_cycles=%d",
        loop_sec,
        logs_dir,
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
            telemetry = _build_telemetry_from_logs(logs_dir)
            if PATTERN_MEMORY_ENABLED:
                try:
                    await _update_pattern_memory(conn, logs_dir)
                except Exception as memory_exc:  # noqa: BLE001
                    LOG.warning("[dynamic-ai] pattern memory skipped: %s", memory_exc)
            prompt = _build_prompt(telemetry)

            try:
                llm_raw = _call_llm(prompt)
                new_cfg = _build_cfg_from_llm(llm_raw, current_cfg)
                decision_source = "llm"
            except Exception as llm_exc:  # noqa: BLE001
                LOG.warning("[dynamic-ai] LLM unavailable (%s) -> heuristic fallback", llm_exc)
                new_cfg = _heuristic_from_telemetry(current_cfg, telemetry)
                decision_source = "heuristic"

            new_cfg = _apply_activity_policy(current_cfg, new_cfg, telemetry)

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
                },
            )
            _save_activity_memory(activity_state_path)
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
