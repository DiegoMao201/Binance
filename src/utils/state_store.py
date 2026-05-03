from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def build_state_snapshot(**kwargs: Any) -> dict[str, Any]:
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **kwargs,
    }


def persist_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def append_history(path: Path, item: dict[str, Any], limit: int = 500) -> None:
    history: list[dict[str, Any]]
    if path.exists():
        history = json.loads(path.read_text(encoding="utf-8"))
    else:
        history = []

    history.append(item)
    trimmed = history[-limit:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trimmed, indent=2, ensure_ascii=False), encoding="utf-8")


def load_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))