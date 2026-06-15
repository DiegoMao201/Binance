from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TRUE_VALUES = {"1", "true", "yes", "on"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _run_validator(validator: Path, logs_dir: Path, check_db: bool) -> tuple[int, dict[str, Any], str]:
    cmd = [sys.executable, str(validator), "--logs-dir", str(logs_dir), "--json",
           "--max-diff-age-sec", "1500"]
    if check_db:
        cmd.append("--check-db")

    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    raw = out if out else err

    try:
        payload = json.loads(out) if out else {"ok": False, "checks": []}
    except Exception:
        payload = {
            "ok": False,
            "checks": [
                {
                    "name": "validator_execution",
                    "ok": False,
                    "detail": f"non-json output: {raw[:800]}",
                }
            ],
        }
    return proc.returncode, payload, raw


def _failed_checks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    checks = payload.get("checks") or []
    if not isinstance(checks, list):
        return []
    failed: list[dict[str, Any]] = []
    for item in checks:
        if not isinstance(item, dict):
            continue
        if item.get("ok") is not True:
            failed.append(item)
    return failed


def _signature(failed: list[dict[str, Any]]) -> str:
    names = sorted(str(item.get("name", "unknown")) for item in failed)
    return "|".join(names)


def _advice_for(name: str) -> str:
    mapping = {
        "heartbeat": "Revisar si el daemon principal sigue vivo y escribiendo deriv_status.json.",
        "dynamic_enabled": "Confirmar DERIV_DYNAMIC_CONFIG_ENABLED=true en runtime del contenedor principal.",
        "dynamic_refresh_interval": "Validar DERIV_DYNAMIC_CONFIG_REFRESH_SEC y que no fue sobreescrito en .env.",
        "dynamic_last_refresh": "Revisar conectividad DB y logs del daemon para [dynamic-config] refreshed.",
        "dynamic_symbols_loaded": "Verificar tabla dynamic_symbol_config y carga de simbolos en status.",
        "diff_log_freshness": "Revisar sidecar IA (fase-6: debe escribir heartbeat cada ~1200s). Reiniciar sidecar si mtime_age > 1500s.",
        "diff_log_json_parse": "Revisar corrupcion del JSONL de diffs y limpiar entradas truncadas.",
        "diff_log_recent_activity": "Validar que el orquestador IA este arriba y ejecutando ciclos (fase-6: escribe heartbeat aunque hysteresis bloquee cambios).",
        "env_escape_valve": "Post-838e3cd (2026-06-12): DERIV_BOOM_CRASH_ESCAPE_VALVE debe ser TRUE (soft penalty -0.20 sin FVG). Si aparece este fallo es porque esta en false (politica FASE-6 obsoleta). Verificar si la regresion fue intencional.",
        "env_block_boom600": "FASE-6: DERIV_BLOCK_BC_ESCAPE_BOOM600 debe estar UNSET (sin definir). Si aparece este fallo es porque esta definida con valor incorrecto.",
        "env_block_boom900": "FASE-6: DERIV_BLOCK_BC_ESCAPE_BOOM900 debe estar UNSET (sin definir). Si aparece este fallo es porque esta definida con valor incorrecto.",
        "env_hysteresis_window": "Ajustar DYNAMIC_AI_MIN_STATE_LIFETIME_SEC (recomendado >= 600).",
        "db_guardrails": "Corregir filas fuera de rango en dynamic_symbol_config o constraints en DB.",
        "validator_execution": "Revisar ejecucion del validador (dependencias/ruta Python/salida JSON).",
    }
    return mapping.get(name, "Revisar logs del servicio y el payload de validacion para este check.")


def _render_fail_message(
    *,
    failed: list[dict[str, Any]],
    payload: dict[str, Any],
    raw_output: str,
    reason: str,
) -> str:
    lines: list[str] = []
    lines.append("ALERTA CRITICA: DERIV WATCHDOG EN FAIL")
    lines.append(f"Hora UTC: {_now_iso()}")
    lines.append(f"Motivo de alerta: {reason}")
    lines.append("")
    lines.append("Checks fallidos:")
    for item in failed:
        name = str(item.get("name", "unknown"))
        detail = str(item.get("detail", "sin detalle"))
        lines.append(f"- {name}: {detail}")
        lines.append(f"  Accion: {_advice_for(name)}")

    lines.append("")
    lines.append("Resumen operativo:")
    lines.append("- El sistema NO esta en estado PASS.")
    lines.append("- Esta alerta se dispara para reaccion temprana y evitar fallos silenciosos.")

    if raw_output and not isinstance(payload, dict):
        lines.append("")
        lines.append(f"Salida validador: {raw_output[:500]}")

    message = "\n".join(lines)
    return message[:3900]


def _render_recovery_message() -> str:
    return "\n".join(
        [
            "RECOVERY: DERIV WATCHDOG VOLVIO A PASS",
            f"Hora UTC: {_now_iso()}",
            "Todos los checks del validador estan OK.",
        ]
    )[:3900]


def _send_telegram(message: str) -> tuple[bool, str]:
    enabled = _as_bool(os.getenv("TELEGRAM_ENABLED"), default=False)
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

    if not enabled:
        return False, "telegram_disabled"
    if not token or not chat_id:
        return False, "telegram_missing_credentials"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": message[:4000],
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            _ = resp.read()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"telegram_error: {exc}"


def _send_webhook(message: str, payload: dict[str, Any]) -> tuple[bool, str]:
    url = (os.getenv("WATCHDOG_ALERT_WEBHOOK_URL") or "").strip()
    if not url:
        return False, "webhook_not_configured"

    event = {
        "event": "deriv_runtime_watchdog",
        "ts": _now_iso(),
        "message": message,
        "validator": payload,
    }

    raw = json.dumps(event, ensure_ascii=True).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method="POST")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            _ = resp.read()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"webhook_error: {exc}"


def _should_alert(
    *,
    prev_status: str,
    prev_signature: str,
    prev_alert_ts: float,
    current_status: str,
    current_signature: str,
    reminder_seconds: int,
) -> tuple[bool, str]:
    now = time.time()
    if current_status == "FAIL":
        if prev_status != "FAIL":
            return True, "transition_pass_to_fail"
        if current_signature != prev_signature:
            return True, "fail_signature_changed"
        if (now - prev_alert_ts) >= reminder_seconds:
            return True, "fail_reminder"
        return False, "fail_no_new_signal"

    if current_status == "PASS" and prev_status == "FAIL":
        return True, "recovery_fail_to_pass"
    return False, "no_alert_needed"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Active watchdog wrapper for Deriv runtime validator.")
    parser.add_argument(
        "--validator",
        default="/opt/deriv-watchdog/validate_deriv_runtime.py",
        help="Absolute path to validate_deriv_runtime.py",
    )
    parser.add_argument("--logs-dir", default="/data/deriv-logs")
    parser.add_argument(
        "--state-file",
        default="/data/deriv-logs/deriv_runtime_watchdog_state.json",
        help="Persisted watchdog state for anti-spam transitions",
    )
    parser.add_argument("--check-db", action="store_true", help="Enable DB guardrail validation")
    parser.add_argument(
        "--alert-reminder-min",
        type=int,
        default=30,
        help="Reminder cadence in minutes while FAIL persists",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validator = Path(args.validator)
    logs_dir = Path(args.logs_dir)
    state_path = Path(args.state_file)
    reminder_seconds = max(1, int(args.alert_reminder_min)) * 60

    if not validator.exists():
        print(f"[watchdog] validator_missing path={validator}")
        return 2

    rc, payload, raw_output = _run_validator(validator, logs_dir, args.check_db)
    failed = _failed_checks(payload)
    current_status = "PASS" if payload.get("ok") is True and rc == 0 else "FAIL"
    current_signature = _signature(failed)

    prev = _load_json(
        state_path,
        {
            "status": "UNKNOWN",
            "signature": "",
            "last_alert_ts": 0.0,
            "last_run_ts": None,
        },
    )
    prev_status = str(prev.get("status", "UNKNOWN"))
    prev_signature = str(prev.get("signature", ""))
    prev_alert_ts = float(prev.get("last_alert_ts", 0.0) or 0.0)

    alert_needed, alert_reason = _should_alert(
        prev_status=prev_status,
        prev_signature=prev_signature,
        prev_alert_ts=prev_alert_ts,
        current_status=current_status,
        current_signature=current_signature,
        reminder_seconds=reminder_seconds,
    )

    now_ts = time.time()
    telegram_status = "not_sent"
    webhook_status = "not_sent"

    if alert_needed:
        if current_status == "FAIL":
            msg = _render_fail_message(
                failed=failed,
                payload=payload,
                raw_output=raw_output,
                reason=alert_reason,
            )
        else:
            msg = _render_recovery_message()

        tg_ok, tg_detail = _send_telegram(msg)
        wh_ok, wh_detail = _send_webhook(msg, payload)
        telegram_status = "ok" if tg_ok else tg_detail
        webhook_status = "ok" if wh_ok else wh_detail
        prev["last_alert_ts"] = now_ts

    prev["status"] = current_status
    prev["signature"] = current_signature
    prev["last_run_ts"] = _now_iso()
    prev["last_validator_ok"] = bool(payload.get("ok") is True and rc == 0)
    prev["last_fail_count"] = len(failed)
    _save_json(state_path, prev)

    summary = {
        "ts": _now_iso(),
        "status": current_status,
        "validator_rc": rc,
        "failed_checks": [item.get("name") for item in failed],
        "alert_needed": alert_needed,
        "alert_reason": alert_reason,
        "telegram": telegram_status,
        "webhook": webhook_status,
    }
    print(json.dumps(summary, ensure_ascii=True))

    return 0 if current_status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())