#!/usr/bin/env python3
"""
verify_binance.py — Pre-flight verification for the Binance recorder.

6 checks, all must pass before the recorder is started:
  1. DB connectivity to optiferre_binance as binance_bot
  2. DB isolation: binance_bot cannot read optiferre_pamm
  3. WS connectivity to stream.binance.com (spot) via proxy
  4. WS connectivity to fstream.binance.com (futures) via proxy
  5. pyarrow import and Parquet write/read round-trip
  6. Shadow modules do NOT import TradeExecutor (static AST check)

Usage:
    python scripts/verify_binance.py

Exit code 0 = all checks passed. Non-zero = at least one failed.
"""

import ast
import asyncio
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

PASS = "✓"
FAIL = "✗"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = PASS if ok else FAIL
    results.append((name, ok, detail))
    print(f"  [{status}] {name}" + (f": {detail}" if detail else ""))


# ── Check 1: DB connectivity ──────────────────────────────────────────────────

async def _check_db_connect() -> tuple[bool, str]:
    try:
        import asyncpg
    except ImportError:
        return False, "asyncpg not installed"

    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        return False, "DATABASE_URL not set"
    if "optiferre_pamm" in db_url:
        return False, "DATABASE_URL points to optiferre_pamm — must use optiferre_binance"

    try:
        conn = await asyncpg.connect(db_url, timeout=5)
        row = await conn.fetchrow("SELECT current_database(), current_user")
        await conn.close()
        db_name = row[0]
        user = row[1]
        if db_name != "optiferre_binance":
            return False, f"connected to '{db_name}' — expected 'optiferre_binance'"
        return True, f"connected as {user}@{db_name}"
    except Exception as e:
        return False, str(e)


# ── Check 2: DB isolation ─────────────────────────────────────────────────────

async def _check_db_isolation() -> tuple[bool, str]:
    try:
        import asyncpg
    except ImportError:
        return False, "asyncpg not installed"

    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        return False, "DATABASE_URL not set"

    pamm_url = db_url.replace("optiferre_binance", "optiferre_pamm")
    try:
        conn = await asyncpg.connect(pamm_url, timeout=3)
        await conn.close()
        return False, "binance_bot CAN connect to optiferre_pamm — isolation broken"
    except Exception as e:
        msg = str(e)
        if "permission denied" in msg.lower() or "does not exist" in msg.lower():
            return True, "connection to optiferre_pamm correctly denied"
        return False, f"unexpected error: {msg}"


# ── Check 3: Spot WS connectivity ────────────────────────────────────────────

async def _check_spot_ws() -> tuple[bool, str]:
    try:
        import aiohttp
    except ImportError:
        return False, "aiohttp not installed"

    proxy = os.getenv("BINANCE_PROXY_URL") or None
    url = "wss://stream.binance.com:443/stream?streams=ethfdusd@bookTicker"
    try:
        connector = aiohttp.TCPConnector(ssl=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.ws_connect(
                url, proxy=proxy, timeout=aiohttp.ClientTimeout(total=10, connect=8)
            ) as ws:
                msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    stream = data.get("stream", "?")
                    return True, f"received stream={stream}"
                return False, f"unexpected msg type: {msg.type}"
    except Exception as e:
        return False, str(e)


# ── Check 4: Futures WS connectivity ─────────────────────────────────────────

async def _check_futures_ws() -> tuple[bool, str]:
    """
    When FUTURES_WS_ENABLED=false, verify REST mark price fallback instead.
    fstream.binance.com sends no data from EU/DE proxies (geo-restriction at data level).
    """
    if os.getenv("FUTURES_WS_ENABLED", "true").lower() == "false":
        # Check REST fallback
        try:
            import aiohttp
            proxy = os.getenv("BINANCE_PROXY_URL") or None
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://fapi.binance.com/fapi/v1/premiumIndex",
                    proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    eth = next((r for r in data if r.get("symbol") == "ETHUSDT"), None)
                    if eth:
                        return True, f"REST fallback OK, ETHUSDT markPrice={eth.get('markPrice')}"
                    return False, "REST fallback: ETHUSDT not found in response"
        except Exception as e:
            return False, f"REST fallback: {e}"

    try:
        import aiohttp
    except ImportError:
        return False, "aiohttp not installed"

    proxy = os.getenv("BINANCE_PROXY_URL") or None
    url = "wss://fstream.binance.com/stream?streams=ethusdt@markPrice@1s"
    try:
        connector = aiohttp.TCPConnector(ssl=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.ws_connect(
                url, proxy=proxy, timeout=aiohttp.ClientTimeout(total=10, connect=8)
            ) as ws:
                msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    stream = data.get("stream", "?")
                    return True, f"received stream={stream}"
                return False, f"unexpected msg type: {msg.type}"
    except Exception as e:
        return False, str(e)


# ── Check 5: pyarrow round-trip ───────────────────────────────────────────────

def _check_pyarrow() -> tuple[bool, str]:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return False, "pyarrow not installed — add to requirements.txt"

    try:
        schema = pa.schema([
            pa.field("ts_us", pa.int64()),
            pa.field("price", pa.float64()),
            pa.field("symbol", pa.string()),
        ])
        table = pa.table(
            {"ts_us": [1234567890], "price": [1000.5], "symbol": ["ETHFDUSD"]},
            schema=schema,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.parquet")
            pq.write_table(table, path, compression="zstd")
            read_back = pq.read_table(path)
            assert read_back.num_rows == 1
            assert read_back.column("symbol")[0].as_py() == "ETHFDUSD"
        return True, f"pyarrow {pa.__version__}, round-trip OK"
    except Exception as e:
        return False, str(e)


# ── Check 6: Shadow modules don't import TradeExecutor ───────────────────────

def _check_shadow_isolation() -> tuple[bool, str]:
    shadow_dir = REPO_ROOT / "services" / "shadow"
    if not shadow_dir.exists():
        return False, f"services/shadow/ not found at {shadow_dir}"

    violations = []
    for py_file in shadow_dir.rglob("*.py"):
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except Exception as e:
            violations.append(f"{py_file.name}: parse error: {e}")
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                else:
                    module = ""
                names = [alias.name for alias in node.names]
                combined = module + " " + " ".join(names)
                if "TradeExecutor" in combined or "trader" in module.lower():
                    violations.append(
                        f"{py_file.name}:{node.lineno}: imports '{combined.strip()}'"
                    )

    if violations:
        return False, "TradeExecutor import found: " + "; ".join(violations)
    return True, "no TradeExecutor imports in services/shadow/"


# ── Runner ────────────────────────────────────────────────────────────────────

async def main() -> int:
    print("\nBinance recorder pre-flight verification")
    print("=" * 48)

    print("\n[1] DB connectivity (optiferre_binance)")
    ok, detail = await _check_db_connect()
    check("DB connect", ok, detail)

    print("\n[2] DB isolation (binance_bot ↛ optiferre_pamm)")
    ok, detail = await _check_db_isolation()
    check("DB isolation", ok, detail)

    print("\n[3] Spot WebSocket (stream.binance.com via proxy)")
    ok, detail = await _check_spot_ws()
    check("Spot WS", ok, detail)

    print("\n[4] Futures WebSocket (fstream.binance.com via proxy)")
    ok, detail = await _check_futures_ws()
    check("Futures WS", ok, detail)

    print("\n[5] pyarrow Parquet round-trip")
    ok, detail = _check_pyarrow()
    check("pyarrow", ok, detail)

    print("\n[6] Shadow module isolation (no TradeExecutor)")
    ok, detail = _check_shadow_isolation()
    check("Shadow isolation", ok, detail)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{'=' * 48}")
    print(f"Result: {passed}/{total} checks passed")

    if passed < total:
        print("\nFailed checks:")
        for name, ok, detail in results:
            if not ok:
                print(f"  {FAIL} {name}: {detail}")
        return 1
    else:
        print("All checks passed — recorder is ready to start.")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
