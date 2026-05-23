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

SCORE_MIN_GUARDRAIL = 6.5
SCORE_MAX_GUARDRAIL = 8.0
ZERO_PEAK_FLOOR_BY_SYMBOL = {
    "BOOM500": 60,
    "CRASH500": 60,
    "CRASH600": 60,
}

# Short-term memory to prevent regime oscillation.
STATE_MEMORY: dict[str, dict[str, Any]] = {}
MIN_STATE_LIFETIME_SEC = max(
    480,
    min(int(os.getenv("DYNAMIC_AI_MIN_STATE_LIFETIME_SEC", "600") or 600), 720),
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
    return SymbolCfg(
        regime=regime,
        spike_pre_filter_target=max(50, min(int(cfg.spike_pre_filter_target), 500)),
        zero_peak_grace_sec=max(zero_peak_floor, min(int(cfg.zero_peak_grace_sec), 120)),
        score_min_override=max(SCORE_MIN_GUARDRAIL, min(float(cfg.score_min_override), SCORE_MAX_GUARDRAIL)),
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

        telemetry[sym] = {
            "spikes_15m": len(recent),
            "entered_spikes_15m": len(entered),
            "blocked_spikes_15m": len(blocked),
            "entry_rate_pct": (len(entered) / len(recent) * 100.0) if recent else 0.0,
            "top_block_reasons": block_reasons.most_common(5),
            "contracts_15m": len(close_rows),
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

        spf = base.spike_pre_filter_target
        grace = base.zero_peak_grace_sec
        score = base.score_min_override

        lag_med = t.get("entry_lag_sec_median")
        late120 = float(t.get("late_entry_ge120_pct") or 0.0)
        early80 = float(t.get("next_spike_after_close_le80_pct") or 0.0)

        if regime == "FAST" or (lag_med is not None and lag_med > 120) or late120 >= 35.0:
            spf = int(spf * 0.60)
            score = score - 1.2
        elif regime == "SLOW":
            spf = int(spf * 1.20)
            score = score + 0.7

        if early80 >= 20.0:
            grace = grace + 50

        out[sym] = _clamp_cfg(sym, SymbolCfg(
            regime=regime,
            spike_pre_filter_target=spf,
            zero_peak_grace_sec=grace,
            score_min_override=score,
            is_active=True,
        ))
    return out


def _build_prompt(telemetry_json: dict[str, Any]) -> str:
    return (
        "Actua como un motor cuantitativo de alta frecuencia para indices sinteticos Deriv.\n"
        "Tu objetivo es ajustar los parametros de trading en tiempo real para evitar entradas tardias y salidas prematuras.\n\n"
        "DATOS DE TELEMETRIA (Ultimos 15 mins):\n"
        f"{json.dumps(telemetry_json, ensure_ascii=True)}\n\n"
        "REGLAS DE AJUSTE:\n"
        "1. Si entry_lag_sec es >120s o el mercado esta FAST, reduce spike_pre_filter_target y baja score_min_override de forma moderada sin romper estructura.\n"
        "2. Si hay alta tasa de zero_peak_exit seguida de spike en <80s, aumenta zero_peak_grace_sec entre 60-120s.\n"
        "3. Si el mercado esta SLOW, sube spike_pre_filter_target y sube score_min_override.\n"
        "4. Mantener guardrails estrictos: spike_pre_filter_target [50,500], score_min_override [6.5,8.0].\n"
        "5. Guardrail de riesgo confirmado: BOOM500/CRASH500/CRASH600 deben mantener zero_peak_grace_sec >= 60.\n\n"
        "6. Evitar cambiar de regime continuamente; privilegia estabilidad macro y cambios por bloques temporales.\n\n"
        "Devuelve UNICAMENTE un JSON valido sin markdown ni texto extra con esta forma:\n"
        "{\n"
        "  \"BOOM1000\": {\"regime\": \"FAST\", \"spike_pre_filter_target\": 120, \"zero_peak_grace_sec\": 60, \"score_min_override\": 6.8, \"is_active\": true}\n"
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
        out[sym] = _clamp_cfg(sym, SymbolCfg(
            regime=str(payload.get("regime", cur.regime)).upper(),
            spike_pre_filter_target=int(payload.get("spike_pre_filter_target", cur.spike_pre_filter_target)),
            zero_peak_grace_sec=int(payload.get("zero_peak_grace_sec", cur.zero_peak_grace_sec)),
            score_min_override=float(payload.get("score_min_override", cur.score_min_override)),
            is_active=bool(payload.get("is_active", cur.is_active)),
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
    async with conn.transaction():
        for sym, cfg in cfgs.items():
            previous = current_cfg.get(sym)
            if previous is not None and not _cfg_changed(previous, cfg):
                continue

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
                cfg.regime,
                cfg.spike_pre_filter_target,
                cfg.zero_peak_grace_sec,
                cfg.score_min_override,
                cfg.is_active,
            )
            updates += 1

            if previous is not None:
                diffs = _cfg_diff_items(previous, cfg)
            else:
                diffs = [f"created: {_cfg_to_dict(cfg)}"]
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
                        "new": _cfg_to_dict(cfg),
                    },
                )
    return updates


async def run_loop() -> None:
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL is required")

    loop_raw = os.getenv("DYNAMIC_AI_LOOP_SEC") or os.getenv("DYNAMIC_AI_INTERVAL_SEC") or "60"
    loop_sec = max(20, int(loop_raw or 60))
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

    import asyncpg  # noqa: PLC0415

    LOG.info("[dynamic-ai] starting loop interval=%ss logs_dir=%s", loop_sec, logs_dir)

    while True:
        started = time.time()
        conn = None
        try:
            conn = await asyncpg.connect(dsn, timeout=12.0)
            current_cfg = await _read_current_cfg(conn)
            _seed_state_memory(current_cfg)
            telemetry = _build_telemetry_from_logs(logs_dir)
            prompt = _build_prompt(telemetry)

            try:
                llm_raw = _call_llm(prompt)
                new_cfg = _build_cfg_from_llm(llm_raw, current_cfg)
                decision_source = "llm"
            except Exception as llm_exc:  # noqa: BLE001
                LOG.warning("[dynamic-ai] LLM unavailable (%s) -> heuristic fallback", llm_exc)
                new_cfg = _heuristic_from_telemetry(current_cfg, telemetry)
                decision_source = "heuristic"

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
        except Exception as exc:  # noqa: BLE001
            LOG.exception("[dynamic-ai] loop failure: %s", exc)
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
