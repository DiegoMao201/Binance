"""
PASO 2.3h — Monitor periódico (cron cada 6h).
Reporta WR+PnL por símbolo, distribución bucket, uso capas LLM, PM crecimiento.
"""
from __future__ import annotations
import json, os, time, asyncio, asyncpg
from collections import defaultdict, Counter
from datetime import datetime, timezone


WINDOW_HOURS = int(os.getenv("MONITOR_WINDOW_HOURS", "72"))
LOG_DIR = os.getenv("LOGS_DIR", "/data/logs")
DB_URL = os.getenv("DATABASE_URL", "")


async def run(window_hours: int = WINDOW_HOURS) -> None:
    t0 = time.time() - window_hours * 3600
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"\n{'='*60}")
    print(f"MONITOR PASO 2.3h — {now_str}")
    print(f"Ventana: últimas {window_hours}h")
    print(f"{'='*60}\n")

    # ── 1. Trades ────────────────────────────────────────────────
    closed_path = os.path.join(LOG_DIR, "deriv_closed_contracts.json")
    trades = json.load(open(closed_path)) if os.path.exists(closed_path) else []
    recent = [t for t in trades if float(t.get("closed_at_ts") or 0) >= t0]

    by_sym: dict[str, list] = defaultdict(list)
    for t in recent:
        sym = str(t.get("symbol") or "")
        if sym:
            by_sym[sym].append(float(t.get("realized_pnl_usdt") or 0))

    total_n = sum(len(v) for v in by_sym.values())
    total_pnl = sum(sum(v) for v in by_sym.values())
    total_wins = sum(sum(1 for x in v if x > 0) for v in by_sym.values())

    print(f"── TRADES ({total_n} en {window_hours}h) ──────────────────────────────")
    print(f"{'Symbol':<12} {'N':>5} {'WR':>6} {'PnL':>8}")
    print("-" * 35)
    for sym in sorted(by_sym):
        v = by_sym[sym]
        n = len(v)
        wr = sum(1 for x in v if x > 0) / n * 100 if n else 0
        pnl = sum(v)
        wr_icon = "✅" if wr >= 50 else ("⚠️" if wr >= 40 else "🔴")
        print(f"{sym:<12} {n:>5} {wr_icon}{wr:>4.0f}% {pnl:>+7.2f}")
    print("-" * 35)
    global_wr = total_wins / total_n * 100 if total_n else 0
    wr_icon = "✅" if global_wr >= 50 else ("⚠️" if global_wr >= 45 else "🔴")
    print(f"{'TOTAL':<12} {total_n:>5} {wr_icon}{global_wr:>4.0f}% {total_pnl:>+7.2f}")

    # ── 2. Decisiones ────────────────────────────────────────────
    decisions: list[dict] = []
    dec_path = os.path.join(LOG_DIR, "decisions_enriched.jsonl")
    if os.path.exists(dec_path):
        with open(dec_path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if not d.get("type") and float(d.get("ts") or 0) >= t0:
                        decisions.append(d)
                except Exception:
                    pass

    print(f"\n── DECISIONES ({len(decisions)}) ─────────────────────────────────")

    # Bucket distribution
    buckets = Counter(
        (d.get("score_breakdown") or {}).get("progressive_imminence_bucket")
        for d in decisions
    )
    total_dec = max(1, len(decisions))
    print("Distribución buckets:")
    for b, n in buckets.most_common():
        if b:
            pct = n / total_dec * 100
            print(f"  {b:<20}: {n:>4} ({pct:.0f}%)")

    # LLM layer citation
    kw = {
        "V2":  ["bucket", "scarcity", "triple lock", "imminence", "elasticit"],
        "PM":  ["pattern memory", "pattern_memory", "insufficient data", "historical", "win rate"],
        "MAT": ["maturity", "elapsed", "mature", "ratio", "time since"],
        "GEO": ["structural level", "horizontal level", "diagonal", "trendline", "zone libre"],
    }
    with_reason = [d for d in decisions if (d.get("llm") or {}).get("reason")]
    n_r = max(1, len(with_reason))
    print("\nUso capas LLM:")
    for cat, kws in kw.items():
        cnt = sum(1 for d in with_reason if any(k in (d["llm"]["reason"] or "").lower() for k in kws))
        pct = cnt / n_r * 100
        icon = "✅" if pct >= 50 else ("⚠️" if pct >= 25 else "🔴")
        print(f"  {icon} {cat}: {cnt}/{n_r} ({pct:.0f}%)")

    # PM captured
    pm_cap = sum(1 for d in decisions if d.get("pattern_memory") is not None)
    print(f"\nPM en decision_logger: {pm_cap}/{len(decisions)} ({pm_cap/total_dec*100:.0f}%)")

    # ── 3. Pattern Memory ────────────────────────────────────────
    if DB_URL:
        try:
            url = DB_URL.replace("postgresql://", "postgres://")
            conn = await asyncpg.connect(url)
            total_pm = await conn.fetchval("SELECT COUNT(*) FROM ai_entry_pattern_memory")
            useful = await conn.fetchval(
                "SELECT COUNT(*) FROM ai_entry_pattern_memory WHERE sample_trades >= 5"
            )
            winners = await conn.fetchval(
                "SELECT COUNT(*) FROM ai_entry_pattern_memory WHERE sample_trades >= 5 AND win_rate >= 60"
            )
            losers = await conn.fetchval(
                "SELECT COUNT(*) FROM ai_entry_pattern_memory WHERE sample_trades >= 5 AND win_rate < 40"
            )
            n20 = await conn.fetchval(
                "SELECT COUNT(*) FROM ai_entry_pattern_memory WHERE sample_trades >= 20"
            )
            await conn.close()
            print(f"\n── PATTERN MEMORY ──────────────────────────────────────────")
            print(f"  Total filas:      {total_pm}")
            print(f"  Útiles (n≥5):     {useful}")
            print(f"  Con n≥20:         {n20}")
            print(f"  Ganadores(WR≥60): {winners}")
            print(f"  Perdedores(WR<40):{losers}")
        except Exception as e:
            print(f"\n[PM ERROR] {e}")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(run())
