"""
scripts/migrate_db.py
─────────────────────────────────────────────────────────────────────────────
Aplica idempotentemente la migración 005 (broker discriminator) sobre la
base de datos de producción y opcionalmente resuelve el UUID del usuario
PAMM por email (DERIV_USER_EMAIL).

Se llama desde el entrypoint del container Deriv ANTES de arrancar el daemon:
    python scripts/migrate_db.py && python -m src.main_deriv
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_LOG = logging.getLogger("migrate_db")

# Load .env if present (for local dev)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env", override=False)
except ImportError:
    pass


async def run_migration(conn) -> None:  # type: ignore[type-arg]
    """Apply migration 005 idempotently."""
    sql_path = Path(__file__).parent.parent / "db" / "migrations" / "005_broker_discriminator.sql"
    if not sql_path.exists():
        _LOG.warning("Migration file not found: %s — skipping", sql_path)
        return

    sql = sql_path.read_text(encoding="utf-8")
    _LOG.info("Applying migration 005_broker_discriminator …")
    try:
        await conn.execute(sql)
        _LOG.info("Migration 005 applied OK")
    except Exception as exc:  # noqa: BLE001
        exc_str = str(exc)
        # InsufficientPrivilegeError means the tables are owned by a superuser
        # (supabase/postgres role) and the migration was already applied in a
        # prior deploy with elevated privileges.  Treat as a non-fatal warning
        # so the daemon can still start.
        if "InsufficientPrivilege" in type(exc).__name__ or "must be owner" in exc_str:
            _LOG.warning("Migration 005 skipped (privilege): %s — assuming already applied", exc_str)
        else:
            _LOG.error("Migration 005 failed: %s", exc)
            raise


async def resolve_user_id(conn, email: str) -> str | None:
    """Return the UUID string for a user email, or None if not found."""
    row = await conn.fetchrow("SELECT id FROM users WHERE email = $1", email)
    return str(row["id"]) if row else None


async def main() -> int:
    import asyncpg  # noqa: PLC0415

    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        _LOG.error("DATABASE_URL not set — cannot run migration")
        return 1

    try:
        conn = await asyncpg.connect(db_url, timeout=15)
    except Exception as exc:  # noqa: BLE001
        _LOG.error("Cannot connect to database: %s", exc)
        return 1

    try:
        await run_migration(conn)

        # If DERIV_USER_EMAIL set and DERIV_USER_ID not yet set, resolve + print
        email = os.getenv("DERIV_USER_EMAIL", "").strip()
        uid   = os.getenv("DERIV_USER_ID", "").strip()
        if email and not uid:
            resolved = await resolve_user_id(conn, email)
            if resolved:
                _LOG.info("RESOLVED DERIV_USER_ID=%s  (from email %s)", resolved, email)
                # Write to a file the daemon can read at startup
                out_path = Path(os.getenv("LOGS_DIR", "/data/logs")) / "deriv_user_id.txt"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(resolved)
                _LOG.info("Wrote DERIV_USER_ID to %s", out_path)
            else:
                _LOG.warning("No user found for email %s", email)
    finally:
        await conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
