from __future__ import annotations

import json
import os
import time
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, min_v: int | None = None, max_v: int | None = None) -> int:
    raw = os.getenv(name)
    try:
        val = int(str(raw).strip()) if raw is not None else int(default)
    except Exception:
        val = int(default)
    if min_v is not None:
        val = max(min_v, val)
    if max_v is not None:
        val = min(max_v, val)
    return val


def _env_float(name: str, default: float, min_v: float | None = None, max_v: float | None = None) -> float:
    raw = os.getenv(name)
    try:
        val = float(str(raw).strip()) if raw is not None else float(default)
    except Exception:
        val = float(default)
    if min_v is not None:
        val = max(min_v, val)
    if max_v is not None:
        val = min(max_v, val)
    return val


FEEDBACK_ENABLED = _env_bool("DYNAMIC_AI_24H_FEEDBACK_ENABLED", True)
FEEDBACK_INTERVAL_SEC = _env_int("DYNAMIC_AI_24H_FEEDBACK_INTERVAL_SEC", 24 * 3600, min_v=3600, max_v=7 * 24 * 3600)
FEEDBACK_WINDOW_SEC = _env_int("DYNAMIC_AI_24H_WINDOW_SEC", 24 * 3600, min_v=6 * 3600, max_v=72 * 3600)
FEEDBACK_MIN_TRADES = _env_int("DYNAMIC_AI_24H_MIN_TRADES", 12, min_v=3, max_v=200)
FEEDBACK_BIN_MIN_TRADES = _env_int("DYNAMIC_AI_24H_BIN_MIN_TRADES", 6, min_v=2, max_v=100)
FEEDBACK_SCORE_STEP = _env_float("DYNAMIC_AI_24H_SCORE_STEP", 0.15, min_v=0.02, max_v=0.50)
FEEDBACK_SCORE_MAX_ABS = _env_float("DYNAMIC_AI_24H_SCORE_MAX_ABS", 0.45, min_v=0.05, max_v=1.50)
FEEDBACK_TARGET_WR = _env_float("DYNAMIC_AI_24H_TARGET_WR", 52.0, min_v=35.0, max_v=75.0)
FEEDBACK_TARGET_AVG_PNL = _env_float("DYNAMIC_AI_24H_TARGET_AVG_PNL", 0.0, min_v=-1.0, max_v=1.0)
FEEDBACK_USE_ATR_PERCENTILE = _env_bool("DYNAMIC_AI_24H_USE_ATR_PERCENTILE", True)
FEEDBACK_CTX_LOOKBACK_SEC = _env_int("DYNAMIC_AI_24H_CTX_LOOKBACK_SEC", 900, min_v=60, max_v=3600)
FEEDBACK_OPEN_PROB_SHARE_DISCOUNT = _env_float("DYNAMIC_AI_24H_OPEN_PROB_SHARE_DISCOUNT", 0.35, min_v=0.10, max_v=1.0)
FEEDBACK_OPEN_PROB_DISCOUNT_FACTOR = _env_float("DYNAMIC_AI_24H_OPEN_PROB_DISCOUNT_FACTOR", 0.60, min_v=0.0, max_v=1.0)


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _safe_ts(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
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


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def _load_json(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, list) else []
    except Exception:
        return []


def _score_margin(row: dict[str, Any]) -> float | None:
    sb = row.get("score_breakdown") if isinstance(row.get("score_breakdown"), dict) else {}
    score_raw = _safe_float(sb.get("score_raw"))
    if score_raw is None:
        score_raw = _safe_float(row.get("score"))
    eff = _safe_float(sb.get("effective_min_score"))
    if eff is None:
        eff = _safe_float(sb.get("effective_min"))
    if eff is None:
        eff = _safe_float(row.get("effective_min_score"))
    if score_raw is None or eff is None:
        return None
    return float(score_raw - eff)


def _exit_reason_prefix(row: dict[str, Any]) -> str:
    raw = str(row.get("exit_reason") or row.get("close_reason") or "").strip()
    if not raw:
        return "NA"
    return raw.split(":", 1)[0]


def _build_ctx_atr_index(market_ctx: list[dict[str, Any]], min_ts: float) -> dict[str, tuple[list[float], list[float]]]:
    by_sym: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in market_ctx:
        sym = str(row.get("symbol") or "").upper()
        ts = _safe_ts(row.get("ts") or row.get("timestamp") or row.get("iso"))
        atr_pctile = _safe_float(row.get("atr_percentile"))
        if not sym or ts is None or atr_pctile is None:
            continue
        if ts >= (min_ts - FEEDBACK_CTX_LOOKBACK_SEC):
            by_sym[sym].append((ts, atr_pctile))
    out: dict[str, tuple[list[float], list[float]]] = {}
    for sym, pairs in by_sym.items():
        pairs.sort(key=lambda x: x[0])
        out[sym] = ([x[0] for x in pairs], [x[1] for x in pairs])
    return out


def _lookup_atr_percentile(
    ctx_idx: dict[str, tuple[list[float], list[float]]],
    symbol: str,
    entry_ts: float,
) -> float | None:
    pack = ctx_idx.get(symbol)
    if pack is None:
        return None
    times, vals = pack
    if not times:
        return None
    i = bisect_right(times, entry_ts) - 1
    if i < 0:
        return None
    if (entry_ts - times[i]) > FEEDBACK_CTX_LOOKBACK_SEC:
        return None
    return vals[i]


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=True, indent=2), encoding="utf-8")
    tmp.replace(path)


def should_refresh(state: dict[str, Any], now_ts: float | None = None) -> bool:
    if not FEEDBACK_ENABLED:
        return False
    ts_now = float(now_ts if isinstance(now_ts, (int, float)) else time.time())
    last = _safe_float(state.get("updated_at_ts"))
    if last is None:
        return True
    return (ts_now - last) >= FEEDBACK_INTERVAL_SEC


def has_pending_apply(state: dict[str, Any]) -> bool:
    if not FEEDBACK_ENABLED:
        return False
    updated = _safe_float(state.get("updated_at_ts"))
    applied = _safe_float(state.get("applied_at_ts"))
    if updated is None:
        return False
    return applied is None or abs(applied - updated) > 1e-9


def mark_applied(state: dict[str, Any], now_ts: float | None = None) -> dict[str, Any]:
    updated = _safe_float(state.get("updated_at_ts"))
    if updated is None:
        return state
    out = dict(state)
    out["applied_at_ts"] = updated
    out["applied_at"] = _iso(updated)
    ts_now = float(now_ts if isinstance(now_ts, (int, float)) else time.time())
    out["last_apply_ack_ts"] = ts_now
    out["last_apply_ack"] = _iso(ts_now)
    return out


def compute_feedback_state(
    logs_dir: Path,
    symbols: list[str],
    now_ts: float | None = None,
) -> dict[str, Any]:
    symbols_u = [str(s or "").upper() for s in symbols if str(s or "").strip()]
    closed_rows = _load_json(logs_dir / "deriv_closed_contracts.json")
    market_ctx_rows = _load_json(logs_dir / "deriv_market_context.json")

    now_candidates = [float(now_ts) if isinstance(now_ts, (int, float)) else 0.0]
    for row in closed_rows:
        now_candidates.append(_safe_ts(row.get("closed_at_ts") or row.get("closed_at") or 0.0) or 0.0)
    for row in market_ctx_rows:
        now_candidates.append(_safe_ts(row.get("ts") or row.get("timestamp") or row.get("iso") or 0.0) or 0.0)
    window_end_ts = max(now_candidates) or time.time()
    window_start_ts = window_end_ts - FEEDBACK_WINDOW_SEC

    ctx_idx = _build_ctx_atr_index(market_ctx_rows, window_start_ts)

    stats: dict[str, dict[str, Any]] = {
        sym: {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "pnl_total": 0.0,
            "loss_exit": Counter(),
            "win_exit": Counter(),
            "score_margin_low": [],
            "score_margin_high": [],
            "atr_low": [],
            "atr_high": [],
        }
        for sym in symbols_u
    }

    for row in closed_rows:
        sym = str(row.get("symbol") or "").upper()
        if sym not in stats:
            continue

        opened_ts = _safe_ts(row.get("opened_at_ts") or row.get("opened_at"))
        closed_ts = _safe_ts(row.get("closed_at_ts") or row.get("closed_at"))
        if (opened_ts is None or opened_ts < window_start_ts) and (closed_ts is None or closed_ts < window_start_ts):
            continue

        pnl = _safe_float(row.get("realized_pnl_usdt")) or 0.0
        s = stats[sym]
        s["trades"] += 1
        s["pnl_total"] += pnl
        if pnl > 0:
            s["wins"] += 1
            s["win_exit"][_exit_reason_prefix(row)] += 1
        elif pnl < 0:
            s["losses"] += 1
            s["loss_exit"][_exit_reason_prefix(row)] += 1

        margin = _score_margin(row)
        if margin is not None:
            if margin > 0.6:
                s["score_margin_high"].append(pnl)
            elif margin >= 0.0:
                s["score_margin_low"].append(pnl)

        if FEEDBACK_USE_ATR_PERCENTILE and opened_ts is not None:
            atr_pctile = _lookup_atr_percentile(ctx_idx, sym, opened_ts)
            if atr_pctile is not None:
                if atr_pctile < 30.0:
                    s["atr_low"].append(pnl)
                elif atr_pctile > 70.0:
                    s["atr_high"].append(pnl)

    symbol_payload: dict[str, dict[str, Any]] = {}

    for sym in symbols_u:
        s = stats[sym]
        trades = int(s["trades"])
        wins = int(s["wins"])
        losses = int(s["losses"])
        pnl_total = float(s["pnl_total"])
        win_rate = (float(wins) / float(trades) * 100.0) if trades else 0.0
        avg_pnl = (pnl_total / float(trades)) if trades else 0.0

        margin_low = list(s["score_margin_low"])
        margin_high = list(s["score_margin_high"])
        atr_low = list(s["atr_low"])
        atr_high = list(s["atr_high"])

        margin_low_avg = _avg(margin_low)
        margin_high_avg = _avg(margin_high)
        atr_low_avg = _avg(atr_low)
        atr_high_avg = _avg(atr_high)

        open_prob_exit_losses = int(s["loss_exit"].get("open_prob_exit", 0))
        broker_sl_hit_losses = int(s["loss_exit"].get("broker_sl_hit", 0))
        timeout_multispike_wins = int(s["win_exit"].get("timeout_multispike", 0))

        score_delta = 0.0
        regime_bias = "NONE"
        reasons: list[str] = []

        if trades < FEEDBACK_MIN_TRADES:
            reasons.append(f"insufficient_sample:{trades}<{FEEDBACK_MIN_TRADES}")
        else:
            if win_rate < FEEDBACK_TARGET_WR and avg_pnl < FEEDBACK_TARGET_AVG_PNL:
                score_delta += FEEDBACK_SCORE_STEP
                reasons.append("wr_ev_below_target")

            if len(margin_low) >= FEEDBACK_BIN_MIN_TRADES and len(margin_high) >= FEEDBACK_BIN_MIN_TRADES:
                if (margin_low_avg or 0.0) < -0.005 and (margin_high_avg or 0.0) > ((margin_low_avg or 0.0) + 0.03):
                    score_delta += FEEDBACK_SCORE_STEP
                    reasons.append("score_margin_gap")

            if FEEDBACK_USE_ATR_PERCENTILE and len(atr_low) >= FEEDBACK_BIN_MIN_TRADES and len(atr_high) >= FEEDBACK_BIN_MIN_TRADES:
                if (atr_high_avg or 0.0) < -0.005 and (atr_low_avg or 0.0) > ((atr_high_avg or 0.0) + 0.03):
                    score_delta += FEEDBACK_SCORE_STEP
                    regime_bias = "SLOW"
                    reasons.append("high_volatility_drag")

            if win_rate >= (FEEDBACK_TARGET_WR + 8.0) and avg_pnl > (FEEDBACK_TARGET_AVG_PNL + 0.02):
                score_delta -= FEEDBACK_SCORE_STEP * 0.5
                reasons.append("positive_recovery")

            if losses > 0 and score_delta > 0.0:
                open_prob_share = float(open_prob_exit_losses) / float(losses)
                if open_prob_share >= FEEDBACK_OPEN_PROB_SHARE_DISCOUNT:
                    score_delta *= FEEDBACK_OPEN_PROB_DISCOUNT_FACTOR
                    reasons.append("discount_open_prob_legacy")

            if regime_bias == "NONE" and win_rate < (FEEDBACK_TARGET_WR - 8.0) and avg_pnl < -0.04:
                regime_bias = "SLOW"
                reasons.append("broad_negative_edge")

        score_delta = max(-FEEDBACK_SCORE_MAX_ABS, min(score_delta, FEEDBACK_SCORE_MAX_ABS))
        if abs(score_delta) < 1e-6:
            score_delta = 0.0

        symbol_payload[sym] = {
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 4),
            "pnl_total": round(pnl_total, 6),
            "avg_pnl": round(avg_pnl, 6),
            "open_prob_exit_losses": open_prob_exit_losses,
            "broker_sl_hit_losses": broker_sl_hit_losses,
            "timeout_multispike_wins": timeout_multispike_wins,
            "margin_low_trades": len(margin_low),
            "margin_low_avg_pnl": None if margin_low_avg is None else round(margin_low_avg, 6),
            "margin_high_trades": len(margin_high),
            "margin_high_avg_pnl": None if margin_high_avg is None else round(margin_high_avg, 6),
            "atr_low_trades": len(atr_low),
            "atr_low_avg_pnl": None if atr_low_avg is None else round(atr_low_avg, 6),
            "atr_high_trades": len(atr_high),
            "atr_high_avg_pnl": None if atr_high_avg is None else round(atr_high_avg, 6),
            "score_delta": round(score_delta, 4),
            "regime_bias": regime_bias,
            "reasons": reasons,
            "top_loss_exit": [
                {"reason": k, "count": int(v)}
                for k, v in s["loss_exit"].most_common(4)
            ],
            "top_win_exit": [
                {"reason": k, "count": int(v)}
                for k, v in s["win_exit"].most_common(4)
            ],
        }

    total_trades = sum(int(symbol_payload[s]["trades"]) for s in symbols_u)
    total_wins = sum(int(symbol_payload[s]["wins"]) for s in symbols_u)
    total_losses = sum(int(symbol_payload[s]["losses"]) for s in symbols_u)
    total_pnl = sum(float(symbol_payload[s]["pnl_total"]) for s in symbols_u)

    state: dict[str, Any] = {
        "version": 1,
        "updated_at_ts": float(window_end_ts),
        "updated_at": _iso(window_end_ts),
        "window_start_ts": float(window_start_ts),
        "window_start": _iso(window_start_ts),
        "window_end_ts": float(window_end_ts),
        "window_end": _iso(window_end_ts),
        "applied_at_ts": None,
        "applied_at": None,
        "config": {
            "enabled": FEEDBACK_ENABLED,
            "interval_sec": FEEDBACK_INTERVAL_SEC,
            "window_sec": FEEDBACK_WINDOW_SEC,
            "min_trades": FEEDBACK_MIN_TRADES,
            "score_step": FEEDBACK_SCORE_STEP,
            "score_max_abs": FEEDBACK_SCORE_MAX_ABS,
            "target_wr": FEEDBACK_TARGET_WR,
            "target_avg_pnl": FEEDBACK_TARGET_AVG_PNL,
            "use_atr_percentile": FEEDBACK_USE_ATR_PERCENTILE,
        },
        "totals": {
            "trades": int(total_trades),
            "wins": int(total_wins),
            "losses": int(total_losses),
            "win_rate": round((float(total_wins) / float(total_trades) * 100.0), 4) if total_trades else 0.0,
            "pnl_total": round(float(total_pnl), 6),
        },
        "symbols": symbol_payload,
    }
    return state


def compact_adjustments(state: dict[str, Any]) -> dict[str, Any]:
    symbols = state.get("symbols") if isinstance(state.get("symbols"), dict) else {}
    out: dict[str, Any] = {}
    for sym, payload in symbols.items():
        if not isinstance(payload, dict):
            continue
        delta = _safe_float(payload.get("score_delta")) or 0.0
        bias = str(payload.get("regime_bias") or "NONE").upper()
        if abs(delta) > 1e-9 or bias != "NONE":
            out[sym] = {
                "score_delta": round(delta, 4),
                "regime_bias": bias,
                "reasons": payload.get("reasons") if isinstance(payload.get("reasons"), list) else [],
            }
    return out
