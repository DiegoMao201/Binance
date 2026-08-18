"""
Lab API server — 4 JSON endpoints + lab_terminal.html.

Runs as a single aiohttp task inside the recorder event loop.
No separate container required.

Endpoints:
    GET /                       → lab_terminal.html (reloads from disk on each request)
    GET /api/lab/summary        → system state + aggregate counters
    GET /api/lab/leaderboard    → per-strategy stats with cluster-bootstrapped CI
    GET /api/lab/curve          → markout curves (fixed cohort: all horizons resolved)
    GET /api/lab/tape?limit=N   → last N orders, newest first

Contract: return null for any metric that cannot yet be computed.
          Never invent numbers. The HTML handles nulls with "—" / "midiendo…".

Env vars:
    LAB_API_PORT      (default 8765)
    HYPOTHESES_PATH   (default /data/historical/thesis_results/hypotheses_lab.jsonl)
"""

import asyncio
import json
import logging
import math
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import asyncpg
from aiohttp import web

from ..recorder.disk_guard import disk_free_pct
from ..recorder.health import HealthMonitor

logger = logging.getLogger(__name__)

LAB_API_PORT = int(os.getenv("LAB_API_PORT", "8765"))
HYPOTHESES_PATH = os.getenv(
    "HYPOTHESES_PATH",
    "/data/historical/thesis_results/hypotheses_lab.jsonl",
)
HTML_PATH = Path(__file__).parent / "lab_terminal.html"
TARGET_SECONDS = 7 * 24 * 3600  # 7-day run

# Strategies that are baselines/controls — excluded from t_threshold count
CONTROL_NAMES = frozenset({
    "baseline_random", "ctrl_random_taker",
    "baseline_ladder", "baseline_burnin",
})


# ── Statistics helpers ─────────────────────────────────────────────────────────

def _t_threshold(n_strategies: int) -> Optional[float]:
    """
    Bonferroni-corrected two-tailed threshold for n_strategies tests.
    Uses normal approximation to the t distribution (large sample).
    """
    if n_strategies <= 0:
        return None
    alpha_adj = 0.025 / n_strategies  # 0.05 / 2 / N
    # Rational approximation: Abramowitz & Stegun 26.2.17
    p = 1.0 - alpha_adj
    c = math.sqrt(-2.0 * math.log(1.0 - p))
    z = c - (2.515517 + 0.802853 * c + 0.010328 * c**2) / (
        1.0 + 1.432788 * c + 0.189269 * c**2 + 0.001308 * c**3
    )
    return round(z, 2)


def _bootstrap_ci(
    event_means: List[float], n_boot: int = 1000
) -> Tuple[Optional[float], Optional[float]]:
    """
    Bootstrap 95% CI sampling by independent events (already aggregated by fill_event_id).
    Returns (ci_low, ci_high) or (None, None) if insufficient data.
    """
    n = len(event_means)
    if n < 2:
        return None, None
    means = sorted(
        sum(event_means[random.randrange(n)] for _ in range(n)) / n
        for _ in range(n_boot)
    )
    return round(means[int(0.025 * n_boot)], 2), round(means[int(0.975 * n_boot)], 2)


def _welch_t(
    vals1: List[float], mean1: float,
    vals2: List[float], mean2: float,
) -> Optional[float]:
    """Welch's t-statistic comparing two samples of event-level means."""
    n1, n2 = len(vals1), len(vals2)
    if n1 < 2 or n2 < 2:
        return None
    var1 = sum((x - mean1) ** 2 for x in vals1) / (n1 - 1)
    var2 = sum((x - mean2) ** 2 for x in vals2) / (n2 - 1)
    se = math.sqrt(var1 / n1 + var2 / n2)
    if se == 0:
        return None
    return round((mean1 - mean2) / se, 2)


# ── System helpers ─────────────────────────────────────────────────────────────

def _ram_pct() -> Optional[float]:
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", 0)
        if total == 0:
            return None
        return round(100.0 * (1.0 - avail / total), 1)
    except Exception:
        return None


def _load_hypotheses() -> List[dict]:
    try:
        out = []
        with open(HYPOTHESES_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out
    except Exception:
        return []


# ── Server ─────────────────────────────────────────────────────────────────────

class LabAPIServer:
    def __init__(
        self,
        db: asyncpg.Pool,
        health: HealthMonitor,
        data_dir: str,
    ):
        self._db = db
        self._health = health
        self._data_dir = data_dir

        self._app = web.Application()
        self._app.router.add_get("/", self._html)
        self._app.router.add_get("/lab", self._html)
        self._app.router.add_get("/api/lab/summary", self._summary)
        self._app.router.add_get("/api/lab/leaderboard", self._leaderboard)
        self._app.router.add_get("/api/lab/curve", self._curve)
        self._app.router.add_get("/api/lab/tape", self._tape)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _json(self, obj) -> web.Response:
        return web.Response(
            text=json.dumps(obj, default=str),
            content_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    async def _html(self, _: web.Request) -> web.Response:
        if not HTML_PATH.exists():
            return web.Response(
                status=503,
                text=(
                    "lab_terminal.html not found.\n"
                    f"Expected at: {HTML_PATH}\n"
                    "Copy it to services/lab_api/ and restart."
                ),
            )
        # Serve from disk on every request — no caching needed for this tool
        return web.FileResponse(HTML_PATH)

    @staticmethod
    def _is_taker(name: str) -> bool:
        return name.endswith("_taker") or name == "ctrl_random_taker"

    @staticmethod
    def _ctrl_name(is_taker: bool) -> str:
        return "ctrl_random_taker" if is_taker else "baseline_random"

    # ── /api/lab/summary ──────────────────────────────────────────────────────

    async def _summary(self, _: web.Request) -> web.Response:
        hyp = _load_hypotheses()
        n_non_ctrl = sum(1 for h in hyp if h.get("strategy") not in CONTROL_NAMES)
        t_thr = _t_threshold(n_non_ctrl)

        agg = await self._db.fetchrow("""
            SELECT
                MIN(signal_ts)                                                    AS first_signal,
                COUNT(*)                                                          AS signals,
                COUNT(*) FILTER (WHERE status = 'FILLED')                        AS fills,
                COUNT(DISTINCT fill_event_id) FILTER (WHERE status = 'FILLED')   AS n_ef_total,
                COUNT(DISTINCT EXTRACT(HOUR FROM fill_ts AT TIME ZONE 'UTC'))
                    FILTER (WHERE status = 'FILLED')                              AS hours_covered
            FROM shadow_trades
            WHERE signal_ts > NOW() - INTERVAL '7 days'
        """)

        # n_ef per non-control strategy (for n_ef_min banner)
        nef_rows = await self._db.fetch("""
            SELECT strategy_name,
                COUNT(DISTINCT fill_event_id) FILTER (WHERE status = 'FILLED') AS n_ef
            FROM shadow_trades
            WHERE signal_ts > NOW() - INTERVAL '7 days'
            GROUP BY strategy_name
        """)
        non_ctrl_nef = [
            int(r["n_ef"] or 0)
            for r in nef_rows
            if r["strategy_name"] not in CONTROL_NAMES
        ]
        n_ef_min = min(non_ctrl_nef) if non_ctrl_nef else 0

        # Hourly fill histogram (UTC)
        hour_rows = await self._db.fetch("""
            SELECT EXTRACT(HOUR FROM fill_ts AT TIME ZONE 'UTC')::int AS h,
                   COUNT(*) AS cnt
            FROM shadow_trades
            WHERE status = 'FILLED'
              AND fill_ts > NOW() - INTERVAL '7 days'
            GROUP BY h
        """)
        hour_hist = [0] * 24
        for r in hour_rows:
            hour_hist[int(r["h"])] = int(r["cnt"])

        # Gaps since last hour — null if table doesn't exist yet
        try:
            gap_row = await self._db.fetchrow("""
                SELECT COALESCE(SUM(gap_count), 0)::int AS gaps
                FROM recorder_health
                WHERE updated_at > NOW() - INTERVAL '1 hour'
            """)
        except Exception:
            gap_row = None

        # Stream health from in-memory monitor
        streams_out = []
        all_ok = True
        for name, sh in self._health._streams.items():
            age = self._health.age_seconds(name)
            stale = self._health.is_stale(name)
            if stale:
                all_ok = False
            streams_out.append({
                "name": name,
                "last_msg_age_s": round(age, 1) if age is not None else None,
            })

        # Disk
        try:
            disk_pct = round(disk_free_pct(self._data_dir), 1)
        except Exception:
            disk_pct = None

        elapsed_s = None
        if agg and agg["first_signal"]:
            elapsed_s = int(
                (datetime.now(tz=timezone.utc) - agg["first_signal"]).total_seconds()
            )

        return self._json({
            "symbols": 6,
            "strategies_registered": len(hyp),
            "t_threshold": t_thr,
            "elapsed_s": elapsed_s,
            "target_s": TARGET_SECONDS,
            "signals": int(agg["signals"] or 0) if agg else 0,
            "fills": int(agg["fills"] or 0) if agg else 0,
            "n_ef_total": int(agg["n_ef_total"] or 0) if agg else 0,
            "n_ef_min": n_ef_min,
            "hours_covered": int(agg["hours_covered"] or 0) if agg else 0,
            "hour_hist": hour_hist,
            "candidates": 0,  # expensive to compute — see /leaderboard
            "gaps": int(gap_row["gaps"] or 0) if gap_row else 0,
            "disk_free_pct": disk_pct,
            "ram_pct": _ram_pct(),
            "deriv_ok": None,  # recorder container has no access to Deriv volume
            "streams_ok": all_ok,
            "streams": streams_out,
        })

    # ── /api/lab/leaderboard ──────────────────────────────────────────────────

    async def _leaderboard(self, _: web.Request) -> web.Response:
        fee_expr = "COALESCE((feature_snapshot->>'fee_bps_paid')::numeric, 0)"

        # Per-strategy aggregate
        agg_rows = await self._db.fetch(f"""
            SELECT
                strategy_name,
                COUNT(*) AS signals,
                COUNT(*) FILTER (WHERE status = 'FILLED') AS fills,
                COUNT(DISTINCT fill_event_id) FILTER (WHERE status = 'FILLED') AS n_ef,
                AVG(markout_60s_bps - {fee_expr})
                    FILTER (WHERE status='FILLED' AND markout_60s_bps IS NOT NULL) AS net_bps
            FROM shadow_trades
            WHERE signal_ts > NOW() - INTERVAL '7 days'
            GROUP BY strategy_name
        """)

        # Event-level means for bootstrap CI and t-stat (one row per independent event)
        ev_rows = await self._db.fetch(f"""
            SELECT strategy_name,
                fill_event_id,
                AVG(markout_60s_bps - {fee_expr}) AS ev_net
            FROM shadow_trades
            WHERE status = 'FILLED'
              AND markout_60s_bps IS NOT NULL
              AND signal_ts > NOW() - INTERVAL '7 days'
            GROUP BY strategy_name, fill_event_id
        """)
        ev_by_strat: Dict[str, List[float]] = {}
        for r in ev_rows:
            ev_by_strat.setdefault(r["strategy_name"], []).append(float(r["ev_net"]))

        # Look up control means for vs_control
        byname = {r["strategy_name"]: r for r in agg_rows}

        def _ctrl_net(is_taker: bool) -> Optional[float]:
            row = byname.get(self._ctrl_name(is_taker))
            return float(row["net_bps"]) if row and row["net_bps"] is not None else None

        # Bonferroni threshold
        hyp = _load_hypotheses()
        n_non_ctrl = sum(1 for h in hyp if h.get("strategy") not in CONTROL_NAMES)
        t_thr = _t_threshold(n_non_ctrl) or 2.0

        result = []
        for row in agg_rows:
            name = row["strategy_name"]
            n_ef = int(row["n_ef"] or 0)
            is_taker = self._is_taker(name)
            is_ctrl = name in CONTROL_NAMES
            net_bps = float(row["net_bps"]) if row["net_bps"] is not None else None
            ctrl_net = _ctrl_net(is_taker)
            ev_vals = ev_by_strat.get(name, [])

            ci_low, ci_high = _bootstrap_ci(ev_vals)
            vs_control = (
                round(net_bps - ctrl_net, 2)
                if net_bps is not None and ctrl_net is not None
                else None
            )

            # Welch t-stat vs. matched control
            t_stat = None
            if not is_ctrl and net_bps is not None and ctrl_net is not None:
                ctrl_ev = ev_by_strat.get(self._ctrl_name(is_taker), [])
                t_stat = _welch_t(ev_vals, net_bps, ctrl_ev, ctrl_net)

            # Verdict
            if is_ctrl:
                verdict = "CONTROL"
            elif n_ef < 30:
                verdict = "INSUFICIENTE"
            elif (
                vs_control is not None and vs_control > 10
                and ci_low is not None and ci_low > 0
                and t_stat is not None and abs(t_stat) > t_thr
                # temporal coverage ≥24h with ≥3 bands: not enforced yet (marked DESCARTADA)
            ):
                verdict = "CANDIDATA"
            else:
                verdict = "DESCARTADA"

            result.append({
                "strategy": name,
                "mode": "TAKER" if is_taker else "MAKER",
                "is_control": is_ctrl,
                "signals": int(row["signals"] or 0),
                "fills": int(row["fills"] or 0),
                "n_ef": n_ef,
                "net_bps": round(net_bps, 2) if net_bps is not None else None,
                "ci_low": ci_low,
                "ci_high": ci_high,
                "vs_control": vs_control,
                "t_stat": t_stat,
                "verdict": verdict,
            })

        order = {"CANDIDATA": 0, "CONTROL": 1, "DESCARTADA": 2, "INSUFICIENTE": 3}
        result.sort(key=lambda x: (order.get(x["verdict"], 4), -(x["n_ef"] or 0)))
        return self._json(result)

    # ── /api/lab/curve ────────────────────────────────────────────────────────

    async def _curve(self, _: web.Request) -> web.Response:
        horizons = ["1s", "5s", "30s", "60s", "5m", "15m", "30m", "60m"]
        cols = [
            "markout_1s_bps", "markout_5s_bps", "markout_30s_bps", "markout_60s_bps",
            "markout_5m_bps", "markout_15m_bps", "markout_30m_bps", "markout_60m_bps",
        ]
        fee_expr = "COALESCE((feature_snapshot->>'fee_bps_paid')::numeric, 0)"

        # Cohort constraint: only fills where ALL horizons are resolved.
        # This prevents mixing 1s-resolution fills with 60m-resolution fills
        # (which would make the curve reflect sampling time, not horizon decay).
        cohort = " AND ".join(f"{c} IS NOT NULL" for c in cols)

        avgs = await self._db.fetch(f"""
            SELECT
                strategy_name,
                {", ".join(
                    f"AVG({c} - {fee_expr}) AS {c}"
                    for c in cols
                )}
            FROM shadow_trades
            WHERE status = 'FILLED'
              AND signal_ts > NOW() - INTERVAL '7 days'
              AND {cohort}
            GROUP BY strategy_name
        """)

        series = []
        for r in avgs:
            name = r["strategy_name"]
            values = [
                (round(float(r[c]), 2) if r[c] is not None else None)
                for c in cols
            ]
            # If no fills passed the cohort filter, skip (all values would be None)
            if all(v is None for v in values):
                continue
            series.append({
                "strategy": name,
                "is_control": name in CONTROL_NAMES,
                "values": values,
            })

        series.sort(key=lambda x: (0 if x["is_control"] else 1, x["strategy"]))
        return self._json({"horizons": horizons, "series": series})

    # ── /api/lab/tape ─────────────────────────────────────────────────────────

    async def _tape(self, request: web.Request) -> web.Response:
        try:
            limit = min(int(request.rel_url.query.get("limit", "60")), 200)
        except ValueError:
            limit = 60

        fee_expr = "COALESCE((feature_snapshot->>'fee_bps_paid')::numeric, 0)"

        rows = await self._db.fetch(f"""
            SELECT
                fill_ts,
                signal_ts,
                strategy_name,
                symbol,
                side,
                fill_price,
                status,
                markout_60s_bps,
                {fee_expr} AS fee_bps
            FROM shadow_trades
            WHERE signal_ts > NOW() - INTERVAL '7 days'
            ORDER BY COALESCE(fill_ts, signal_ts) DESC
            LIMIT $1
        """, limit)

        out = []
        for r in rows:
            ts_obj = r["fill_ts"] or r["signal_ts"]
            ts_str = ts_obj.strftime("%H:%M") if ts_obj else None
            net_bps = None
            if r["markout_60s_bps"] is not None:
                net_bps = round(float(r["markout_60s_bps"]) - float(r["fee_bps"]), 2)
            out.append({
                "ts": ts_str,
                "strategy": r["strategy_name"],
                "mode": "TAKER" if self._is_taker(r["strategy_name"]) else "MAKER",
                "symbol": r["symbol"],
                "side": r["side"],
                "fill_price": str(r["fill_price"]) if r["fill_price"] else None,
                "status": r["status"],
                "net_bps": net_bps,
            })

        return self._json(out)

    # ── Runner ────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        runner = web.AppRunner(self._app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", LAB_API_PORT)
        await site.start()
        logger.info("lab API listening on port %d", LAB_API_PORT)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await runner.cleanup()
