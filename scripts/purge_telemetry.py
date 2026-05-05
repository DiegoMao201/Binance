"""Purga la telemetría tras cerrar Trade #2 (BTC contaminado por reglas mixtas).

Resetea closed_trades, signal_history, scan_history, order_history, equity_history y
bot_state.json a estado vacío para que el dashboard de Next.js empiece a calcular el
nuevo Win Rate de equilibrio (~38%) sobre N=1.

Uso:
    ./.venv/bin/python scripts/purge_telemetry.py            # solicita confirmación
    ./.venv/bin/python scripts/purge_telemetry.py --yes      # sin confirmación

NO toca:
    - control.json (estado deseado del bot)
    - open_positions.json si tiene posiciones abiertas (aborta para no romper Mutex)
    - bot.log (los rotados se conservan)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.utils.config import load_settings  # noqa: E402


EMPTY_LIST_FILES = (
    "closed_trades_file",
    "signal_history_file",
    "scan_history_file",
    "order_history_file",
    "equity_history_file",
)


def _backup(path: Path, stamp: str) -> Path | None:
    if not path.exists():
        return None
    backup_dir = path.parent / "backups" / f"purge_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / path.name
    shutil.copy2(path, target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Purga telemetría tras cerrar Trade #2.")
    parser.add_argument("--yes", action="store_true", help="No pedir confirmación.")
    args = parser.parse_args()

    settings = load_settings()
    open_positions_path = Path(settings.open_positions_file)
    if open_positions_path.exists():
        try:
            data = json.loads(open_positions_path.read_text() or "[]")
        except json.JSONDecodeError:
            data = []
        if data:
            print(
                "ERROR: hay posiciones abiertas; cierra primero el Trade #2 antes de purgar.",
                file=sys.stderr,
            )
            return 2

    if not args.yes:
        confirm = input("Esto borrará telemetría histórica. Escribe 'PURGAR' para continuar: ")
        if confirm.strip() != "PURGAR":
            print("Abortado.")
            return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    purged: list[str] = []

    for attr in EMPTY_LIST_FILES:
        path = Path(getattr(settings, attr))
        backup = _backup(path, stamp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]")
        purged.append(f"{path.name}{f' (backup: {backup.name})' if backup else ''}")

    state_path = Path(settings.state_file)
    backup_state = _backup(state_path, stamp)
    if state_path.exists():
        state_path.unlink()
    purged.append(f"{state_path.name}{f' (backup: {backup_state.name})' if backup_state else ''}")

    print("Purga completada. N=1 desde el próximo trade.")
    for entry in purged:
        print(f"  - {entry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
