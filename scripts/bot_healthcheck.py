from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    logs_dir = Path(os.getenv("LOGS_DIR", "/data/logs")).expanduser()
    status_file = logs_dir / "status.json"
    max_age_seconds = int(os.getenv("BOT_HEALTH_MAX_AGE_SECONDS", "180"))

    if not status_file.exists():
        print(f"missing status file: {status_file}")
        return 1

    try:
        payload = json.loads(status_file.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"invalid status file: {exc}")
        return 1

    heartbeat_at = payload.get("heartbeat_at")
    if not heartbeat_at:
        print("missing heartbeat_at")
        return 1

    try:
        heartbeat = datetime.fromisoformat(str(heartbeat_at))
    except ValueError:
        print(f"invalid heartbeat_at: {heartbeat_at}")
        return 1

    age_seconds = (datetime.now(timezone.utc) - heartbeat).total_seconds()
    if age_seconds > max_age_seconds:
        print(f"stale heartbeat: {age_seconds:.0f}s")
        return 1

    print(payload.get("status", "unknown"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
