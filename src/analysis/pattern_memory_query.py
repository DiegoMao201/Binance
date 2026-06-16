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
    t = (fvg_tier_raw or "").lower()
    if "mitigated" in t or "confluence" in t:
        return "mitigated"
    if any(x in t for x in ("detected", "active", "bull", "bear")):
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
