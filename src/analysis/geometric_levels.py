"""
Geometric levels detection — Horizontal supports/resistances and diagonal trendlines.
PASO 2.3h-prep Fase D: auto-detección de niveles estructurales para contexto LLM.

Input: list of prices (floats) — no timestamps needed, indices used as x-axis.
Requirements (Diego philosophy):
  - Horizontals: price touched zone ±tolerance N>=3 times in window
  - Diagonals: N>=3 OBJECTIVE touches, anti-post-hoc validation
  - NO injecting pareidolia
"""
from __future__ import annotations

from typing import Dict, List


def detect_horizontal_levels(
    prices: List[float],
    tolerance_pct: float = 0.0015,
    min_touches: int = 3,
    cluster_radius_pct: float = 0.0005,
    window: int = 2000,
) -> List[Dict]:
    """
    Detecta niveles horizontales con N>=min_touches en los últimos `window` precios.

    Returns list of:
      {'price': float, 'type': 'resistance'|'support',
       'n_touches': int, 'distance_pct': float}
    """
    if not prices or len(prices) < 100:
        return []

    data = prices[-window:]
    current = data[-1]

    # Extremos locales con ventana de 10 ticks
    extrema_max: List[float] = []
    extrema_min: List[float] = []
    half = 5
    for i in range(half, len(data) - half):
        chunk = data[i - half: i + half + 1]
        p = data[i]
        if p == max(chunk):
            extrema_max.append(p)
        if p == min(chunk):
            extrema_min.append(p)

    def cluster(extrema: List[float], radius: float) -> List[List[float]]:
        clusters: List[List[float]] = []
        for p in extrema:
            placed = False
            for cl in clusters:
                avg = sum(cl) / len(cl)
                if abs(p - avg) / avg <= radius:
                    cl.append(p)
                    placed = True
                    break
            if not placed:
                clusters.append([p])
        return clusters

    levels: List[Dict] = []
    for cl in cluster(extrema_max, cluster_radius_pct):
        if len(cl) >= min_touches:
            avg = sum(cl) / len(cl)
            levels.append({
                "price": avg,
                "type": "resistance",
                "n_touches": len(cl),
                "distance_pct": (avg - current) / current * 100,
            })
    for cl in cluster(extrema_min, cluster_radius_pct):
        if len(cl) >= min_touches:
            avg = sum(cl) / len(cl)
            levels.append({
                "price": avg,
                "type": "support",
                "n_touches": len(cl),
                "distance_pct": (avg - current) / current * 100,
            })

    levels.sort(key=lambda l: abs(l["distance_pct"]))
    return levels


def detect_diagonal_trendlines(
    prices: List[float],
    min_touches: int = 3,
    tolerance_pct: float = 0.001,
    window: int = 2000,
) -> List[Dict]:
    """
    Detecta trendlines diagonales con N>=min_touches (anti-post-hoc).

    Returns list of:
      {'type': str, 'n_touches': int,
       'projected_price_now': float, 'distance_pct': float}
    """
    if not prices or len(prices) < 100:
        return []

    data = prices[-window:]
    n = len(data)
    current = data[-1]

    # Extremos locales con ventana de 15 ticks
    extrema_max: List[tuple[int, float]] = []
    extrema_min: List[tuple[int, float]] = []
    half = 7
    for i in range(half, n - half):
        chunk = data[i - half: i + half + 1]
        p = data[i]
        if p == max(chunk):
            extrema_max.append((i, p))
        if p == min(chunk):
            extrema_min.append((i, p))

    def find_trendlines(extrema: List[tuple[int, float]], line_type: str) -> List[Dict]:
        found: List[Dict] = []
        if len(extrema) < min_touches:
            return found
        for a in range(len(extrema) - 1):
            for b in range(a + 1, len(extrema)):
                i1, p1 = extrema[a]
                i2, p2 = extrema[b]
                if i1 == i2:
                    continue
                slope = (p2 - p1) / (i2 - i1)
                if line_type == "descending_resistance" and slope >= 0:
                    continue
                if line_type == "ascending_support" and slope <= 0:
                    continue
                touches = [(i1, p1), (i2, p2)]
                for ix, px in extrema:
                    if (ix, px) in touches:
                        continue
                    proj = p1 + slope * (ix - i1)
                    if abs(px - proj) / abs(proj) <= tolerance_pct:
                        touches.append((ix, px))
                if len(touches) >= min_touches:
                    proj_now = p1 + slope * (n - 1 - i1)
                    found.append({
                        "type": line_type,
                        "n_touches": len(touches),
                        "projected_price_now": proj_now,
                        "distance_pct": (proj_now - current) / current * 100,
                    })
        # Deduplicar por precio proyectado cercano
        unique: List[Dict] = []
        for tl in sorted(found, key=lambda x: -x["n_touches"]):
            dup = any(
                abs(tl["projected_price_now"] - u["projected_price_now"])
                / abs(u["projected_price_now"]) <= 0.001
                for u in unique
            )
            if not dup:
                unique.append(tl)
        return unique

    all_lines = (
        find_trendlines(extrema_max, "descending_resistance")
        + find_trendlines(extrema_min, "ascending_support")
    )
    all_lines.sort(key=lambda l: abs(l["distance_pct"]))
    return all_lines


def get_active_structure(
    symbol: str,
    prices: List[float],
    max_levels: int = 3,
) -> Dict:
    """
    Retorna estructura geométrica activa para el LLM (top-N más cercanos).
    """
    if not prices:
        return {"symbol": symbol, "current_price": None,
                "horizontal_levels": [], "diagonal_trendlines": []}

    horizontals = detect_horizontal_levels(prices)
    diagonals = detect_diagonal_trendlines(prices)

    return {
        "symbol": symbol,
        "current_price": prices[-1],
        "horizontal_levels": horizontals[:max_levels],
        "diagonal_trendlines": diagonals[:max_levels],
        "n_total_horizontals": len(horizontals),
        "n_total_diagonals": len(diagonals),
    }
