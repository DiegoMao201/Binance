"""
Ghost Logger — captura "trades fantasma" cuando un gate duro bloquea
un setup de calidad, y rastrea si el spike hubiera sido GHOST_WIN o GHOST_LOSS.

Formato de registro:
  { id, ts, symbol, price, side, score_raw, gate, setup_type, grade,
    scarcity, imm_state, imm_score, sl_pct, expires_at,
    outcome, outcome_ts, outcome_price }

outcomes: PENDING → GHOST_WIN | GHOST_LOSS | GHOST_EXPIRED
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class GhostTrade:
    id: str
    ts: float
    symbol: str
    price: float
    side: str
    score_raw: float
    gate: str
    setup_type: str
    grade: Optional[str]
    scarcity: Optional[str]
    imm_state: Optional[str]
    imm_score: Optional[float]
    sl_pct: float
    expires_at: float
    stake_usdt: float = 5.0
    outcome: str = "PENDING"
    outcome_ts: Optional[float] = None
    outcome_price: Optional[float] = None
    estimated_pnl: Optional[float] = None


class GhostLogger:
    """
    Thread-safe. Writes are infrequent (only on gate-block and outcome resolution).
    on_tick() is in-memory only — safe to call on every tick.
    """

    MAX_RECORDS = 3000
    SPAM_GUARD_SEC = 120  # max 1 ghost per symbol+gate per 120s

    def __init__(
        self,
        log_dir: str = "/data/logs",
        window_sec: float = 600.0,
        sl_pct: float = 0.025,
    ) -> None:
        self._path = os.path.join(log_dir, "ghost_trades.json")
        self._window = window_sec
        self._sl_pct = sl_pct
        self._pending: dict[str, GhostTrade] = {}
        self._lock = threading.RLock()
        self._load_pending()

    # ── public API ────────────────────────────────────────────────────────────

    def add(
        self,
        symbol: str,
        price: float,
        side: str,
        score_raw: float,
        gate: str,
        setup_type: str,
        grade: Optional[str] = None,
        scarcity: Optional[str] = None,
        imm_state: Optional[str] = None,
        imm_score: Optional[float] = None,
        stake_usdt: float = 5.0,
    ) -> None:
        now = time.time()
        sym = symbol.upper()
        with self._lock:
            # Spam guard: 1 ghost per (symbol, gate) per SPAM_GUARD_SEC
            for gt in self._pending.values():
                if gt.symbol == sym and gt.gate == gate and (now - gt.ts) < self.SPAM_GUARD_SEC:
                    return
            gt = GhostTrade(
                id=uuid.uuid4().hex[:16],
                ts=now,
                symbol=sym,
                price=float(price),
                side=str(side or ""),
                score_raw=float(score_raw),
                gate=str(gate),
                setup_type=str(setup_type or "UNKNOWN"),
                grade=grade,
                scarcity=scarcity,
                imm_state=imm_state,
                imm_score=float(imm_score) if imm_score is not None else None,
                sl_pct=self._sl_pct,
                expires_at=now + self._window,
                stake_usdt=float(stake_usdt),
            )
            self._pending[gt.id] = gt
        self._append(gt)

    def on_tick(self, symbol: str, price: float, last_spike_ts: float = 0.0) -> None:
        """Call on every tick. O(pending_for_symbol) — fast."""
        now = time.time()
        sym = symbol.upper()
        resolved: list[GhostTrade] = []
        with self._lock:
            for gid, gt in list(self._pending.items()):
                if gt.symbol != sym:
                    continue
                # New spike after the ghost was created → WIN
                if last_spike_ts > gt.ts:
                    gt.outcome = "GHOST_WIN"
                    gt.outcome_ts = float(last_spike_ts)
                    gt.outcome_price = float(price)
                    gt.estimated_pnl = self._estimate_pnl(gt, float(price))
                    resolved.append(self._pending.pop(gid))
                    continue
                # Window expired
                if now > gt.expires_at:
                    gt.outcome = "GHOST_EXPIRED"
                    gt.outcome_ts = now
                    gt.outcome_price = float(price)
                    gt.estimated_pnl = 0.0
                    resolved.append(self._pending.pop(gid))
                    continue
                # Adverse price move = virtual SL hit → LOSS
                if gt.side == "MULTDOWN" and price > gt.price * (1.0 + gt.sl_pct):
                    gt.outcome = "GHOST_LOSS"
                    gt.outcome_ts = now
                    gt.outcome_price = float(price)
                    gt.estimated_pnl = -gt.stake_usdt
                    resolved.append(self._pending.pop(gid))
                elif gt.side == "MULTUP" and price < gt.price * (1.0 - gt.sl_pct):
                    gt.outcome = "GHOST_LOSS"
                    gt.outcome_ts = now
                    gt.outcome_price = float(price)
                    gt.estimated_pnl = -gt.stake_usdt
                    resolved.append(self._pending.pop(gid))
        for gt in resolved:
            self._update(gt)

    @staticmethod
    def _estimate_pnl(gt: "GhostTrade", outcome_price: float) -> float:
        """Estimated PnL for a resolved ghost trade using Deriv multiplier formula.

        Multiplier ≈ 1/sl_pct. WIN profit = stake × price_move_pct / sl_pct,
        capped at 3× stake to account for early DPM exit.
        """
        if gt.outcome == "GHOST_LOSS":
            return -gt.stake_usdt
        if gt.outcome != "GHOST_WIN" or gt.price <= 0:
            return 0.0
        if gt.side == "MULTUP":
            move_pct = (outcome_price - gt.price) / gt.price
        else:
            move_pct = (gt.price - outcome_price) / gt.price
        sl = gt.sl_pct if gt.sl_pct > 0 else 0.025
        raw = gt.stake_usdt * max(0.0, move_pct) / sl
        return round(min(raw, gt.stake_usdt * 3.0), 4)

    def stats(self, hours: float = 24.0) -> dict:
        """Aggregated win/loss/expired counts + PnL/PF per gate for the last `hours`."""
        cutoff = time.time() - hours * 3600
        try:
            records = self._read_all()
        except Exception:
            return {}
        out: dict[str, dict] = {}
        for r in records:
            if r.get("ts", 0) < cutoff:
                continue
            g = str(r.get("gate") or "unknown")
            oc = str(r.get("outcome") or "PENDING")
            if g not in out:
                out[g] = {
                    "total": 0, "WIN": 0, "LOSS": 0, "EXPIRED": 0, "PENDING": 0,
                    "gross_win_pnl": 0.0, "gross_loss_pnl": 0.0,
                }
            out[g]["total"] += 1
            pnl = float(r.get("estimated_pnl") or 0.0)
            if oc == "GHOST_WIN":
                out[g]["WIN"] += 1
                out[g]["gross_win_pnl"] += pnl
            elif oc == "GHOST_LOSS":
                out[g]["LOSS"] += 1
                out[g]["gross_loss_pnl"] += abs(pnl)
            elif oc == "GHOST_EXPIRED":
                out[g]["EXPIRED"] += 1
            else:
                out[g]["PENDING"] += 1
        # Compute derived metrics
        for g, d in out.items():
            resolved = d["WIN"] + d["LOSS"]
            d["win_rate"] = round(d["WIN"] / resolved, 3) if resolved > 0 else 0.0
            d["net_pnl"] = round(d["gross_win_pnl"] - d["gross_loss_pnl"], 2)
            d["profit_factor"] = round(
                d["gross_win_pnl"] / d["gross_loss_pnl"], 2
            ) if d["gross_loss_pnl"] > 0 else (999.0 if d["gross_win_pnl"] > 0 else 0.0)
            d["avg_win_pnl"] = round(d["gross_win_pnl"] / d["WIN"], 2) if d["WIN"] > 0 else 0.0
            d["avg_loss_pnl"] = round(d["gross_loss_pnl"] / d["LOSS"], 2) if d["LOSS"] > 0 else 0.0
            d["gross_win_pnl"] = round(d["gross_win_pnl"], 2)
            d["gross_loss_pnl"] = round(d["gross_loss_pnl"], 2)
        return out

    def recent(self, n: int = 50) -> list[dict]:
        """Last `n` resolved ghost trades (newest first)."""
        try:
            records = self._read_all()
        except Exception:
            return []
        resolved = [r for r in records if r.get("outcome") != "PENDING"]
        return list(reversed(resolved[-n:]))

    # ── internal ──────────────────────────────────────────────────────────────

    def _load_pending(self) -> None:
        now = time.time()
        try:
            for r in self._read_all():
                if r.get("outcome") == "PENDING" and r.get("expires_at", 0) > now:
                    fields = {k: r.get(k) for k in GhostTrade.__dataclass_fields__}
                    gt = GhostTrade(**fields)
                    self._pending[gt.id] = gt
        except Exception:
            pass

    def _read_all(self) -> list[dict]:
        if not os.path.exists(self._path):
            return []
        with open(self._path, encoding="utf-8") as f:
            return json.load(f)

    def _write_all(self, records: list[dict]) -> None:
        if len(records) > self.MAX_RECORDS:
            records = records[-self.MAX_RECORDS :]
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(records, f)

    def _append(self, gt: GhostTrade) -> None:
        try:
            records = self._read_all()
            records.append(asdict(gt))
            self._write_all(records)
        except Exception:
            pass

    def _update(self, gt: GhostTrade) -> None:
        try:
            records = self._read_all()
            for r in records:
                if r.get("id") == gt.id:
                    r.update(asdict(gt))
                    break
            self._write_all(records)
        except Exception:
            pass
