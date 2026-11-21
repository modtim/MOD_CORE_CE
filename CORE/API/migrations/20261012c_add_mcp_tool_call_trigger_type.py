"""workflow-productivity-floor 4.5 — add MCP_TOOL_CALL to workflowtriggertype

Idempotent (ADD VALUE IF NOT EXISTS). Additive.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261012c_add_mcp_tool_call_trigger_type.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

NEW_LABELS = ["MCP_TOOL_CALL"]


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (getattr(raw, "driver_connection", None)
             or getattr(raw, "connection", None) or raw)
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()
    stmts = [f"ALTER TYPE workflowtriggertype ADD VALUE IF NOT EXISTS '{lbl}';"
             for lbl in NEW_LABELS]
    if not args.commit:
        print("DRY RUN — would execute:\n" + "\n".join(stmts))
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    for s in stmts:
        cur.execute(s)
    cur.close()
    print(f"workflowtriggertype: ensured labels {NEW_LABELS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
