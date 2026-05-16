"""
scripts/migrate_db.py
─────────────────────────────────────────────────────────────────────────────
Aplica idempotentemente las migraciones 005 y 006 sobre la base de datos de
producción y opcionalmente resuelve el UUID del usuario PAMM por email
(DERIV_USER_EMAIL).

Se llama desde el entrypoint del container Deriv ANTES de arrancar el daemon:
    python scripts/migrate_db.py && python -m src.main_deriv

Cada migración se ejecuta en su propia conexión+transacción independiente.
Un fallo de privilegios en una migración no aborta las siguientes.
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

# Migrations to apply in order. Each runs in its own independent connection
# so a privilege error on one never aborts subsequent migrations.
_MIGRATIONS = [
    "005_broker_discriminator.sql",
    "006_deriv_ticks.sql",
]


async def run_migration(db_url: str, filename: str) -> bool:
    """Apply a single migration file in an isolated connection. Returns True on success."""
    import asyncpg  # noqa: PLC0415

    sql_path = Path(__file__).parent.parent / "db" / "migrations" / filename
    if not sql_path.exists():
        _LOG.warning("Migration file not found: %s — skipping", sql_path)
        return True  # not fatal — file simply not present yet

    sql = sql_path.read_text(encoding="utf-8")
    name = filename.split(".")[0]
    _LOG.info("Applying migration %s ...", name)

    # Fresh connection per migration — a failed transaction on one cannot
    # contaminate subsequent migrations.
    try:
        conn = await asyncpg.connect(db_url, timeout=15)
    except Exception as exc:  # noqa: BLE001
        _LOG.error("Cannot connect to database for %s: %s", name, exc)
        return False

    try:
        await conn.execute(sql)
        _LOG.info("Migration %s applied OK", name)
        return True
    except Exception as exc:  # noqa: BLE001
        exc_str = str(exc)
        if "InsufficientPrivilege" in type(exc).__name__ or "must be owner" in exc_str:
            _LOG.warning(
                "Migration %s skipped (privilege): %s — assuming already applied", name, exc_str
            )
            return True  # non-fatal: table owned by superuser, was applied previously
        _LOG.error("Migration %s failed: %s", name, exc)
        return False
    finally:
        await conn.close()


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

    # Apply each migration independently so a failure in one does not abort others.
    any_hard_failure = False
    for filename in _MIGRATIONS:
        ok = await run_migration(db_url, filename)
        if not ok:
            any_hard_failure = True

    if any_hard_failure:
        _LOG.warning("One or more migrations had hard failures — daemon may start with degraded DB")

    # Resolve DERIV_USER_ID from email if needed — uses its own fresh connection
    # so it is completely isolated from any migration transaction state.
    email = os.getenv("DERIV_USER_EMAIL", "").strip()
    uid   = os.getenv("DERIV_USER_ID", "").strip()
    if email and not uid:
        try:
            conn = await asyncpg.connect(db_url, timeout=15)
            try:
                resolved = await resolve_user_id(conn, email)
            finally:
                await conn.close()

            if resolved:
                _LOG.info("RESOLVED DERIV_USER_ID=%s  (from email %s)", resolved, email)
                out_path = Path(os.getenv("LOGS_DIR", "/data/logs")) / "deriv_user_id.txt"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(resolved)
                _LOG.info("Wrote DERIV_USER_ID to %s", out_path)
            else:
                _LOG.warning("No user found for email %s", email)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("Could not resolve user ID for %s: %s — continuing", email, exc)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
