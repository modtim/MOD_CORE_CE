"""Manager-host disk consumers (launch-completion-megaplan Q.1c, "25.2 Measure").

Read-only snapshot of what is filling the MANAGER host as seen from the
API container: Postgres database size + top tables, the API log dir, the
Harbor/registry data volume (only if mounted into the API container), and
the root filesystem usage. Surfaces on the platform-admin extended health
snapshot (`services.platform_health_extended_service.get_extended_health_snapshot`)
under `database.manager_disk`.

Every sub-measure is independently try/except'd — one failure sets that
field to None and appends to `errors`; this function never raises. File
walks are bounded: symlinks are skipped and the walk stops after
_MAX_FILES files (flagged via `truncated`). The result is cached
in-process for _CACHE_TTL_SECONDS to keep the endpoint cheap.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

_LOG_DIR_ENV = "API_LOG_DIR"
_DEFAULT_LOG_DIR = "/app/logs"
_MAX_FILES = 200_000
_CACHE_TTL_SECONDS = 300.0

_cache_lock = threading.Lock()
_cache_ts = 0.0
_cache_value: Optional[dict] = None


def _log_dir() -> str:
    return os.getenv(_LOG_DIR_ENV, _DEFAULT_LOG_DIR)


def _measure_db(db: Session) -> tuple[Optional[int], Optional[list]]:
    """pg_database_size(current_database()) + top-10 tables by
    pg_total_relation_size over pg_stat_user_tables."""
    db_bytes = None
    top_tables = None
    db_bytes = int(db.execute(
        text("SELECT pg_database_size(current_database())")
    ).scalar_one())
    rows = db.execute(text(
        "SELECT relname, pg_total_relation_size(schemaname || '.' || relname) AS bytes"
        " FROM pg_stat_user_tables ORDER BY bytes DESC LIMIT 10"
    )).all()
    top_tables = [{"table": r[0], "bytes": int(r[1])} for r in rows]
    return db_bytes, top_tables


def _measure_dir_bytes(path: str) -> tuple[Optional[int], bool]:
    """Sum file sizes under `path`. Symlinks skipped. Stops after
    _MAX_FILES files (returns truncated=True)."""
    total = 0
    count = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in files:
            full = os.path.join(root, name)
            if os.path.islink(full):
                continue
            try:
                total += os.path.getsize(full)
            except OSError:
                continue
            count += 1
            if count >= _MAX_FILES:
                return total, True
    return total, False


def _measure_registry() -> tuple[Optional[int], Optional[str]]:
    """Harbor/registry data volume size if a registry data path is
    mounted into the API container; otherwise None + explanatory note."""
    # Known candidate destinations for a Harbor/registry data volume.
    for cand in ("/harbor/data", "/var/lib/registry", "/registry"):
        if os.path.isdir(cand) and not os.path.islink(cand):
            total, _trunc = _measure_dir_bytes(cand)
            return total, None
    return None, ("registry data volume is not mounted into the API "
                  "container; measure it on the host (e.g. "
                  "du -sh <harbor data volume>) instead")


def _measure_fresh(db: Session) -> dict[str, Any]:
    out: dict[str, Any] = {
        "db_bytes": None,
        "top_tables": None,
        "log_dir_bytes": None,
        "registry_bytes": None,
        "registry_note": None,
        "root_fs": None,
        "truncated": False,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "errors": [],
    }
    try:
        db_bytes, top_tables = _measure_db(db)
        out["db_bytes"], out["top_tables"] = db_bytes, top_tables
    except Exception as e:
        out["errors"].append(f"db: {type(e).__name__}: {e}")
    try:
        log_dir = _log_dir()
        if os.path.isdir(log_dir):
            total, truncated = _measure_dir_bytes(log_dir)
            out["log_dir_bytes"] = total
            out["truncated"] = out["truncated"] or truncated
        else:
            out["errors"].append(f"log-dir: no log dir at {log_dir}")
    except Exception as e:
        out["errors"].append(f"log-dir: {type(e).__name__}: {e}")
    try:
        out["registry_bytes"], out["registry_note"] = _measure_registry()
    except Exception as e:
        out["registry_bytes"] = None
        out["errors"].append(f"registry: {type(e).__name__}: {e}")
    try:
        usage = shutil.disk_usage("/")
        out["root_fs"] = {"total": usage.total, "used": usage.used,
                          "free": usage.free}
    except Exception as e:
        out["errors"].append(f"root-fs: {type(e).__name__}: {e}")
    return out


def clear_cache() -> None:
    """Drop the in-process cache (used by tests)."""
    global _cache_ts, _cache_value
    with _cache_lock:
        _cache_ts = 0.0
        _cache_value = None


def measure_manager_disk(db: Session) -> dict[str, Any]:
    """Cached (5 min) manager-host disk measurement. Never raises."""
    global _cache_ts, _cache_value
    now = time.time()
    with _cache_lock:
        if _cache_value is not None and (now - _cache_ts) < _CACHE_TTL_SECONDS:
            return _cache_value
    try:
        value = _measure_fresh(db)
    except Exception as e:  # belt and braces — sub-measures already catch
        value = {"db_bytes": None, "top_tables": None, "log_dir_bytes": None,
                 "registry_bytes": None, "registry_note": None, "root_fs": None,
                 "truncated": False,
                 "measured_at": datetime.now(timezone.utc).isoformat(),
                 "errors": [f"measure: {type(e).__name__}: {e}"]}
    with _cache_lock:
        _cache_value, _cache_ts = value, time.time()
    return value
