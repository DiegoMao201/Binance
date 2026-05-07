from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=2, ensure_ascii=False)
    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


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
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(trimmed, indent=2, ensure_ascii=False)
    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def persist_history(path: Path, history: list[dict[str, Any]], limit: int = 500) -> None:
    trimmed = history[-limit:]
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(trimmed, indent=2, ensure_ascii=False)
    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def load_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _quarantine_corrupted_json(path)
        return []