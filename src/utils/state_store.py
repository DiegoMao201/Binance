from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger("optiferre.state_store")


def _safe_atomic_write(path: Path, payload: str) -> None:
    """Escribe payload en `path` de forma atomica usando rename.

    Si os.replace falla (e.g. el volumen se remonté o el tmp desaparecio),
    cae back a escritura directa para no crashear el bot.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        _log.warning(
            "state_store: atomic write fallo (%s). Fallback a escritura directa en %s.",
            exc,
            path,
        )
        # Fallback: escritura directa. Menos segura pero mantiene el bot vivo.
        try:
            # Limpiar el tmp si existe y pudo quedar a medias
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload, encoding="utf-8")
        except OSError as exc2:
            _log.error(
                "state_store: fallback de escritura tambien fallo para %s: %s. Dato no persistido.",
                path,
                exc2,
            )


def build_state_snapshot(**kwargs: Any) -> dict[str, Any]:
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **kwargs,
    }


def _quarantine_corrupted_json(path: Path) -> None:
    if not path.exists():
        return
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine_path = path.with_name(f"{path.stem}.corrupt.{suffix}{path.suffix}")
    try:
        os.replace(path, quarantine_path)
    except OSError:
        return


def persist_state(path: Path, state: dict[str, Any]) -> None:
    payload = json.dumps(state, indent=2, ensure_ascii=False)
    _safe_atomic_write(path, payload)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _quarantine_corrupted_json(path)
        return {}


def append_history(path: Path, item: dict[str, Any], limit: int = 500) -> None:
    history: list[dict[str, Any]]
    if path.exists():
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            _quarantine_corrupted_json(path)
            history = []
    else:
        history = []

    history.append(item)
    trimmed = history[-limit:]
    payload = json.dumps(trimmed, indent=2, ensure_ascii=False)
    _safe_atomic_write(path, payload)


def persist_history(path: Path, history: list[dict[str, Any]], limit: int = 500) -> None:
    trimmed = history[-limit:]
    payload = json.dumps(trimmed, indent=2, ensure_ascii=False)
    _safe_atomic_write(path, payload)


def load_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _quarantine_corrupted_json(path)
        return []