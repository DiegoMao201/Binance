from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> datetime | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _tail_lines(path: Path, max_lines: int) -> list[str]:
    # Read only the last 256 KB to avoid loading multi-MB JSONL files entirely.
    # JSONL entries are ~200-500 bytes each; 256 KB covers 500+ lines comfortably.
    _CHUNK = 256 * 1024
    try:
        size = path.stat().st_size
        with path.open("rb") as _f:
            if size > _CHUNK:
                _f.seek(-_CHUNK, 2)
                raw = _f.read()
                # First line after seek is likely incomplete — drop it.
                first_nl = raw.find(b"\n")
                raw = raw[first_nl + 1:] if first_nl >= 0 else raw
            else:
                raw = _f.read()
    except OSError:
        raw = b""
    lines = [ln for ln in raw.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    return lines[-max_lines:] if len(lines) > max_lines else lines


async def _check_db_guardrails(database_url: str, min_symbols: int) -> CheckResult:
    try:
        import asyncpg  # type: ignore
    except Exception as exc:  # pragma: no cover
        return CheckResult(
            name="db_guardrails",
            ok=False,
            detail=f"asyncpg not available: {exc}",
        )

    score_min_guardrail = float(os.getenv("DYNAMIC_AI_SCORE_MIN_GUARDRAIL", "5.5") or 5.5)
    score_max_guardrail = float(os.getenv("DYNAMIC_AI_SCORE_MAX_GUARDRAIL", "9.2") or 9.2)
    spf_min_guardrail = max(
        50,
        int(
            os.getenv(
                "DYNAMIC_AI_SPIKE_PREFILTER_MIN_TICKS",
                os.getenv("DERIV_SPIKE_PREFILTER_MIN_TICKS", "50"),
            )
            or 50
        ),
    )
    spf_max_guardrail = max(
        spf_min_guardrail,
        int(
            os.getenv(
                "DYNAMIC_AI_SPIKE_PREFILTER_MAX_TICKS",
                os.getenv("DERIV_SPIKE_PREFILTER_MAX_TICKS", "2500"),
            )
            or 2500
        ),
    )

    query = """
    SELECT
        COUNT(*)::int AS total_rows,
        COUNT(*) FILTER (
            WHERE score_min_override < $1 OR score_min_override > $2
        )::int AS bad_score,
        COUNT(*) FILTER (
            WHERE spike_pre_filter_target < $3 OR spike_pre_filter_target > $4
        )::int AS bad_spf,
        COUNT(*) FILTER (
            WHERE zero_peak_grace_sec < 0 OR zero_peak_grace_sec > 120
        )::int AS bad_grace,
        COUNT(*) FILTER (
            WHERE symbol IN ('BOOM500', 'CRASH500', 'CRASH600')
              AND zero_peak_grace_sec < 60
        )::int AS bad_sensitive_grace
        ,COUNT(*) FILTER (
            WHERE dep_exit_policy NOT IN ('PASSIVE', 'SHADOW', 'ACTIVE_DECAY')
        )::int AS bad_dep_policy
        ,COUNT(*) FILTER (
            WHERE dep_min_hold_sec < 30 OR dep_min_hold_sec > 900
        )::int AS bad_dep_hold
        ,COUNT(*) FILTER (
            WHERE dep_atr_decay_ratio < 0.30 OR dep_atr_decay_ratio > 0.95
        )::int AS bad_dep_ratio
        ,COUNT(*) FILTER (
            WHERE dep_loss_floor_usdt < -5.0 OR dep_loss_floor_usdt > 0.0
        )::int AS bad_dep_loss_floor
    FROM dynamic_symbol_config
    """

    conn = None
    try:
        conn = await asyncpg.connect(database_url)
        row = await conn.fetchrow(
            query,
            score_min_guardrail,
            score_max_guardrail,
            spf_min_guardrail,
            spf_max_guardrail,
        )
    except Exception as exc:
        return CheckResult(name="db_guardrails", ok=False, detail=f"db query failed: {exc}")
    finally:
        if conn is not None:
            await conn.close()

    if row is None:
        return CheckResult(name="db_guardrails", ok=False, detail="dynamic_symbol_config returned no data")

    total_rows = int(row["total_rows"])
    bad_score = int(row["bad_score"])
    bad_spf = int(row["bad_spf"])
    bad_grace = int(row["bad_grace"])
    bad_sensitive_grace = int(row["bad_sensitive_grace"])
    bad_dep_policy = int(row["bad_dep_policy"])
    bad_dep_hold = int(row["bad_dep_hold"])
    bad_dep_ratio = int(row["bad_dep_ratio"])
    bad_dep_loss_floor = int(row["bad_dep_loss_floor"])

    ok = (
        total_rows >= min_symbols
        and bad_score == 0
        and bad_spf == 0
        and bad_grace == 0
        and bad_sensitive_grace == 0
        and bad_dep_policy == 0
        and bad_dep_hold == 0
        and bad_dep_ratio == 0
        and bad_dep_loss_floor == 0
    )

    detail = (
        f"rows={total_rows}, bad_score={bad_score} [range={score_min_guardrail:.2f}..{score_max_guardrail:.2f}], "
        f"bad_spf={bad_spf} [range={spf_min_guardrail}..{spf_max_guardrail}], "
        f"bad_grace={bad_grace}, bad_sensitive_grace={bad_sensitive_grace}, "
        f"bad_dep_policy={bad_dep_policy}, bad_dep_hold={bad_dep_hold}, "
        f"bad_dep_ratio={bad_dep_ratio}, bad_dep_loss_floor={bad_dep_loss_floor}"
    )
    return CheckResult(name="db_guardrails", ok=ok, detail=detail)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Validate Deriv runtime intelligence and guardrails.")
    p.add_argument("--logs-dir", default=os.getenv("LOGS_DIR", "/data/logs"))
    p.add_argument("--status-file", default="deriv_status.json")
    p.add_argument("--diff-file", default="dynamic_ai_config_diffs.jsonl")
    p.add_argument("--max-heartbeat-age-sec", type=int, default=180)
    p.add_argument("--max-dynamic-age-sec", type=int, default=120)
    p.add_argument("--max-refresh-sec", type=int, default=30)
    p.add_argument("--max-diff-age-sec", type=int, default=900)
    p.add_argument("--min-diff-events", type=int, default=1)
    p.add_argument("--min-symbols", type=int, default=8)
    p.add_argument("--tail-lines", type=int, default=300)
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")
    p.add_argument(
        "--check-db",
        action="store_true",
        help="Enable DB guardrail checks (requires DATABASE_URL and asyncpg)",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()

    logs_dir = Path(args.logs_dir).expanduser()
    status_path = logs_dir / args.status_file
    diff_path = logs_dir / args.diff_file
    results: list[CheckResult] = []

    if not status_path.exists():
        results.append(CheckResult("status_file", False, f"missing {status_path}"))
        return _emit_and_exit(results, args.json)

    try:
        status_payload = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception as exc:
        results.append(CheckResult("status_file", False, f"invalid JSON: {exc}"))
        return _emit_and_exit(results, args.json)

    heartbeat = _parse_iso(status_payload.get("heartbeat_at"))
    if heartbeat is None:
        results.append(CheckResult("heartbeat", False, "missing/invalid heartbeat_at"))
    else:
        age = (_now_utc() - heartbeat).total_seconds()
        results.append(
            CheckResult(
                "heartbeat",
                age <= args.max_heartbeat_age_sec,
                f"age={age:.1f}s (max={args.max_heartbeat_age_sec}s)",
            )
        )

    dyn = status_payload.get("dynamic_config") or {}
    dyn_enabled = bool(dyn.get("enabled"))
    results.append(CheckResult("dynamic_enabled", dyn_enabled, f"enabled={dyn_enabled}"))

    refresh_sec = int(dyn.get("refresh_sec") or 0)
    results.append(
        CheckResult(
            "dynamic_refresh_interval",
            refresh_sec > 0 and refresh_sec <= args.max_refresh_sec,
            f"refresh_sec={refresh_sec} (max={args.max_refresh_sec})",
        )
    )

    last_refresh = _parse_iso(dyn.get("last_refresh"))
    if last_refresh is None:
        results.append(CheckResult("dynamic_last_refresh", False, "missing/invalid last_refresh"))
    else:
        age = (_now_utc() - last_refresh).total_seconds()
        results.append(
            CheckResult(
                "dynamic_last_refresh",
                age <= args.max_dynamic_age_sec,
                f"age={age:.1f}s (max={args.max_dynamic_age_sec}s)",
            )
        )

    loaded_symbols = dyn.get("symbols_loaded") or []
    count_symbols = len(loaded_symbols) if isinstance(loaded_symbols, list) else 0
    results.append(
        CheckResult(
            "dynamic_symbols_loaded",
            count_symbols >= args.min_symbols,
            f"symbols_loaded={count_symbols} (min={args.min_symbols})",
        )
    )

    if not diff_path.exists():
        results.append(CheckResult("diff_log_file", False, f"missing {diff_path}"))
    else:
        mtime_age = (_now_utc().timestamp() - diff_path.stat().st_mtime)
        results.append(
            CheckResult(
                "diff_log_freshness",
                mtime_age <= args.max_diff_age_sec,
                f"mtime_age={mtime_age:.1f}s (max={args.max_diff_age_sec}s)",
            )
        )

        diff_events_recent = 0
        parse_errors = 0
        for line in _tail_lines(diff_path, args.tail_lines):
            try:
                row = json.loads(line)
            except Exception:
                parse_errors += 1
                continue
            ts = _parse_iso(row.get("ts"))
            if ts is None:
                continue
            age = (_now_utc() - ts).total_seconds()
            if age <= args.max_diff_age_sec:
                diff_events_recent += 1

        results.append(
            CheckResult(
                "diff_log_json_parse",
                parse_errors == 0,
                f"parse_errors={parse_errors}",
            )
        )
        results.append(
            CheckResult(
                "diff_log_recent_activity",
                diff_events_recent >= args.min_diff_events,
                f"recent_diff_events={diff_events_recent} (min={args.min_diff_events})",
            )
        )

    valve = (os.getenv("DERIV_BOOM_CRASH_ESCAPE_VALVE", "").strip().lower() in TRUE_VALUES)
    block_b600_raw = os.getenv("DERIV_BLOCK_BC_ESCAPE_BOOM600", "").strip().lower()
    block_b900_raw = os.getenv("DERIV_BLOCK_BC_ESCAPE_BOOM900", "").strip().lower()
    block_b600_safe = block_b600_raw in {"", *TRUE_VALUES}
    block_b900_safe = block_b900_raw in {"", *TRUE_VALUES}
    # Post-838e3cd (2026-06-12): true = correcto (soft penalty -0.20 en vez de hard veto).
    # FASE-6 (2026-05-23) requería false, pero fue revertido conscientemente.
    results.append(CheckResult("env_escape_valve", valve, f"DERIV_BOOM_CRASH_ESCAPE_VALVE={valve}"))
    results.append(
        CheckResult(
            "env_block_boom600",
            block_b600_safe,
            f"DERIV_BLOCK_BC_ESCAPE_BOOM600={block_b600_raw or 'unset'}",
        )
    )
    results.append(
        CheckResult(
            "env_block_boom900",
            block_b900_safe,
            f"DERIV_BLOCK_BC_ESCAPE_BOOM900={block_b900_raw or 'unset'}",
        )
    )

    min_lifetime_raw = os.getenv("DYNAMIC_AI_MIN_STATE_LIFETIME_SEC", "600").strip()
    try:
        min_lifetime = int(min_lifetime_raw or "0")
    except ValueError:
        min_lifetime = 0
    results.append(
        CheckResult(
            "env_hysteresis_window",
            min_lifetime >= 300,
            f"DYNAMIC_AI_MIN_STATE_LIFETIME_SEC={min_lifetime}",
        )
    )

    if args.check_db:
        database_url = os.getenv("DATABASE_URL", "").strip()
        if database_url:
            results.append(asyncio.run(_check_db_guardrails(database_url, args.min_symbols)))
        else:
            results.append(CheckResult("db_guardrails", False, "DATABASE_URL not set"))

    return _emit_and_exit(results, args.json)


def _emit_and_exit(results: list[CheckResult], as_json: bool) -> int:
    overall_ok = all(item.ok for item in results)
    payload = {
        "ok": overall_ok,
        "checks": [
            {
                "name": item.name,
                "ok": item.ok,
                "detail": item.detail,
            }
            for item in results
        ],
    }

    if as_json:
        print(json.dumps(payload, ensure_ascii=True))
    else:
        print("DERIV_RUNTIME_VALIDATOR", "PASS" if overall_ok else "FAIL")
        for item in results:
            status = "OK" if item.ok else "FAIL"
            print(f"- [{status}] {item.name}: {item.detail}")

    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())