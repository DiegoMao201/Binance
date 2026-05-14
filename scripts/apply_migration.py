"""
apply_migration.py
──────────────────
Applies a SQL migration file using asyncpg (no psql required).

Usage:
    DATABASE_URL=postgresql://user:pass@host/db \
        python -m scripts.apply_migration db/migrations/001_v_cohort_v3_metrics.sql

    # Dry-run (prints SQL without executing):
    python -m scripts.apply_migration db/migrations/001_v_cohort_v3_metrics.sql --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


async def _apply(sql: str, dsn: str, dry_run: bool) -> int:
    if dry_run:
        print("── DRY-RUN — SQL that would be executed ──────────────────────")
        print(sql)
        print("──────────────────────────────────────────────────────────────")
        return 0

    try:
        import asyncpg
    except ImportError:
        print("ERROR: asyncpg not installed.  Run: pip install 'asyncpg>=0.29'",
              file=sys.stderr)
        return 1

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(sql)
        print("Migration applied successfully.")
    except Exception as exc:
        print(f"ERROR applying migration: {exc}", file=sys.stderr)
        return 1
    finally:
        await conn.close()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply a SQL migration file via asyncpg.")
    parser.add_argument("migration_file", help="Path to the .sql file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the SQL without executing it")
    args = parser.parse_args()

    sql_path = Path(args.migration_file)
    if not sql_path.exists():
        print(f"ERROR: migration file not found: {sql_path}", file=sys.stderr)
        return 1

    sql = sql_path.read_text(encoding="utf-8")

    dsn = os.environ.get("DATABASE_URL")
    if not dsn and not args.dry_run:
        print("ERROR: DATABASE_URL environment variable not set.", file=sys.stderr)
        return 1

    return asyncio.run(_apply(sql, dsn or "", args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
