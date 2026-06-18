"""
Pattern Memory v2 — Query module.
PASO 2.3h-prep Fase C: consumo de Pattern Memory para inyectar
histórico al prompt LLM.

DB: ai_entry_pattern_memory (PostgreSQL via asyncpg).
Dimensiones v2: symbol × side × fvg_tier × hurst_bucket × imminence_state × score_bucket.

Bucketing (v2):
  fvg_tier      : mitigated | detected | none
  hurst_bucket  : persistent (H>0.55) | random (0.45-0.55) | antipersistent (H<0.45)
  imminence_state: ripe_or_overdue | building | fresh_or_dry
  score_bucket  : medio (<6.8) | alto (6.8-8.0) | excelente (>=8.0)
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict

_PM_CACHE: Dict[str, Any] = {"data": {}, "loaded_at": 0.0}
_PM_CACHE_TTL = 300.0  # refrescar cada 5 min


def _hurst_to_bucket(hurst: float) -> str:
    if hurst > 0.55:
        return "persistent"
    if hurst < 0.45:
        return "antipersistent"
    return "random"


def _score_to_bucket(score: float) -> str:
    if score >= 8.0:
        return "excelente"
    if score >= 6.8:
        return "alto"
    return "medio"


def _imminence_to_bucket(state: str | None) -> str:
    s = (state or "").lower()
    if any(x in s for x in ("ripe", "overdue", "extreme")):
        return "ripe_or_overdue"
    if "build" in s:
        return "building"
    return "fresh_or_dry"


def _fvg_to_tier(fvg_tier_raw: str | None) -> str:
    # Must match _fvg_tier_cat() in dynamic_ai_orchestrator.py (authoritative PM writer)
    t = (fvg_tier_raw or "").lower().strip()
    if "mitigat" in t:
        return "mitigated"
    if t and t not in ("none", "unknown", ""):
        return "detected"
    return "none"


async def _load_all_patterns(db_url: str) -> Dict[str, Dict]:
    """Cargar toda la tabla en memoria (50 filas max, rápido)."""
    try:
        import asyncpg
        url = db_url.replace("postgresql://", "postgres://")
        conn = await asyncio.wait_for(asyncpg.connect(url), timeout=5.0)
        try:
            rows = await conn.fetch(
                "SELECT symbol, side, fvg_tier, hurst_bucket, imminence_state, "
                "score_bucket, sample_trades, wins, win_rate, avg_pnl_usdt "
                "FROM ai_entry_pattern_memory"
            )
        finally:
            await conn.close()
        result: Dict[str, Dict] = {}
        for row in rows:
            key = (
                f"{row['symbol']}:{row['side']}:{row['fvg_tier']}:"
                f"{row['hurst_bucket']}:{row['imminence_state']}:{row['score_bucket']}"
            )
            result[key] = {
                "n_trades": row["sample_trades"],
                "wins": row["wins"],
                "wr": float(row["win_rate"]),
                "avg_pnl": float(row["avg_pnl_usdt"]),
            }
        return result
    except Exception:
        return {}


def check_pm_filter(
    symbol: str,
    imminence_state_raw: str | None,
    score: float,
    hurst: float,
    side: str,
    fvg_tier_raw: str | None,
    setup_type: str | None,
) -> Dict[str, Any]:
    """
    PASO 2.7 P.4: Pre-LLM filter based on Pattern Memory.

    Uses in-memory cache (_PM_CACHE). Must call after query_pattern_memory
    has populated the cache at least once; returns PASS if cache is empty.

    Lookup hierarchy:
    1. Exact 6-dim match (symbol+side+fvg_tier+hurst+imminence+score, n>=5)
    2. Aggregate without fvg_tier and side (symbol+hurst+imminence+score, n>=10)
    3. No match → PASS (let LLM decide)

    Block thresholds:
    - WR < 35% AND n >= 10 → BLOCK (confirmed loser)
    - WR < 30% AND n >= 5  → BLOCK (extreme losing with small sample)
    """
    data = _PM_CACHE.get("data") or {}
    if not data:
        return {"action": "PASS", "reason": "cache_empty", "match_level": "none"}

    imminence = _imminence_to_bucket(imminence_state_raw)
    score_bucket = _score_to_bucket(score)
    hurst_bucket = _hurst_to_bucket(hurst)
    fvg_tier = _fvg_to_tier(fvg_tier_raw)
    sym = (symbol or "").upper()
    sd = (side or "").lower()

    # 1. Exact 6-dim match
    exact_key = f"{sym}:{sd}:{fvg_tier}:{hurst_bucket}:{imminence}:{score_bucket}"
    row = data.get(exact_key)
    if row and row.get("n_trades", 0) >= 5:
        verdict = _pm_evaluate(row, "6dim", exact_key, setup_type)
        if verdict["action"] == "BLOCK":
            return verdict

    # 2. Aggregate across side+fvg_tier (symbol+hurst+imminence+score)
    agg_n, agg_wins = 0, 0
    agg_pnl_sum = 0.0
    for k, r in data.items():
        parts = k.split(":")
        if (
            len(parts) == 6
            and parts[0] == sym
            and parts[3] == hurst_bucket
            and parts[4] == imminence
            and parts[5] == score_bucket
        ):
            agg_n += r.get("n_trades", 0)
            agg_wins += r.get("wins", 0)
            agg_pnl_sum += float(r.get("avg_pnl", 0)) * r.get("n_trades", 0)

    if agg_n >= 10:
        agg_row = {
            "n_trades": agg_n,
            "wins": agg_wins,
            "wr": agg_wins / agg_n * 100.0,
            "avg_pnl": agg_pnl_sum / agg_n if agg_n > 0 else 0.0,
        }
        verdict = _pm_evaluate(agg_row, "3dim", f"{sym}|{hurst_bucket}|{imminence}|{score_bucket}", setup_type)
        if verdict["action"] == "BLOCK":
            return verdict

    return {"action": "PASS", "reason": "acceptable_or_insufficient_data", "match_level": "none"}


def _pm_evaluate(row: Dict, match_level: str, pattern_id: str, setup_type: str | None) -> Dict[str, Any]:
    n = row.get("n_trades", 0)
    wins = row.get("wins", 0)
    wr = row.get("wr") if "wr" in row else (wins / n * 100.0 if n > 0 else 0.0)
    avg_pnl = float(row.get("avg_pnl", 0))

    if wr < 35.0 and n >= 10:
        return {
            "action": "BLOCK",
            "reason": f"losing_pattern wr={wr:.0f}% n={n} avg_pnl=${avg_pnl:.3f}",
            "match_level": match_level,
            "pattern_id": pattern_id,
            "setup_type": setup_type,
        }
    if wr < 30.0 and n >= 5:
        return {
            "action": "BLOCK",
            "reason": f"extreme_loser wr={wr:.0f}% n={n}",
            "match_level": match_level,
            "pattern_id": pattern_id,
            "setup_type": setup_type,
        }
    return {
        "action": "PASS",
        "reason": f"acceptable wr={wr:.0f}% n={n}",
        "match_level": match_level,
        "pattern_id": pattern_id,
    }


async def query_pattern_memory(
    symbol: str,
    side: str,
    fvg_tier_raw: str | None,
    imminence_state_raw: str | None,
    hurst: float,
    score: float,
    db_url: str,
    min_n: int = 5,
) -> Dict[str, Any]:
    """Retorna estadísticas históricas del patrón actual. Thread-safe (asyncio)."""
    now = time.time()
    if (now - _PM_CACHE["loaded_at"]) >= _PM_CACHE_TTL or not _PM_CACHE["data"]:
        data = await _load_all_patterns(db_url)
        _PM_CACHE["data"] = data
        _PM_CACHE["loaded_at"] = now

    fvg_tier = _fvg_to_tier(fvg_tier_raw)
    hurst_bucket = _hurst_to_bucket(hurst)
    imminence_state = _imminence_to_bucket(imminence_state_raw)
    score_bucket = _score_to_bucket(score)
    lookup_key = (
        f"{symbol}:{side}:{fvg_tier}:{hurst_bucket}:{imminence_state}:{score_bucket}"
    )
    display_key = f"{fvg_tier}|{hurst_bucket}|{imminence_state}|{score_bucket}"

    row = _PM_CACHE["data"].get(lookup_key)
    if not row or row["n_trades"] < min_n:
        n = row["n_trades"] if row else 0
        return {
            "enough_data": False,
            "pattern_key": display_key,
            "reason": f"n={n}<{min_n}",
        }
    return {
        "enough_data": True,
        "pattern_key": display_key,
        "n_trades": row["n_trades"],
        "wr": row["wr"],
        "avg_pnl": row["avg_pnl"],
    }
