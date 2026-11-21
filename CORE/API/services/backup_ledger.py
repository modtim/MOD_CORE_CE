"""GY.C13b/GY.C11 — shared readers for the backup/restore ledger.

The backup machinery writes its readings next to the erasure ledger:
  * CORE/startup/backup.sh (via lib/backup-offbox.sh) writes
    backup_status.json — last_success_utc / last_attempt_utc / verified /
    overall / last_error (the Q14 "verified backup <= 24h" contract);
  * CORE/startup/restore-test.sh appends
    {"type":"restore_test", ts, backup_id, ok, rto_seconds, rpo_seconds,
      checks{}, error?} lines to the erasure ledger JSONL.

Paths resolve through services.erasure_ledger.resolve_path() (MOD_ERASURE_
LEDGER_PATH override, container default, repo-relative fallback) so tests
can point at a temp ledger; backup_status.json sits next to it. A missing/
unreadable file is NOT a crash: the readers report it (empty + readable=False
/ None) and the callers decide how to degrade. Malformed ledger lines are
skipped — one bad line must not mask good readings.

services/conformance/checks.py and services/evidence_bundle.py both read
through this module so there is exactly one implementation.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

# 1788f CE fallback for the ledger path (mirrors services.erasure_ledger's
# constants; the paid module is absent in the CE carve).
_CE_LEDGER_DEFAULT = "/metadata/erasure_ledger.jsonl"


def parse_utc(value) -> Optional[datetime]:
    """Parse the ISO-8601 'Z' timestamps the shell writers emit. None on
    anything unparseable (null, "", bad format)."""
    if not isinstance(value, str) or not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def ledger_path() -> str:
    """1788f — CE carve: services.erasure_ledger is a paid module, so CE has no
    erasure ledger. The path is still resolved the same way (env override >
    container default > repo-relative fallback) so backup_status.json sits next
    to where the ledger lives; the ledger itself is never claimed —
    read_restore_tests() reports it as unreadable (unknown/manual), which is
    the honest CE default."""
    from utils.paid_import import optional_paid
    _ledger = optional_paid("services.erasure_ledger")
    if _ledger is not None:
        return _ledger.resolve_path()
    env = os.environ.get("MOD_ERASURE_LEDGER_PATH")
    if env:
        return env
    if os.path.isdir(os.path.dirname(_CE_LEDGER_DEFAULT)):
        return _CE_LEDGER_DEFAULT
    return os.path.join(os.getcwd(), "Metadata", "erasure_ledger.jsonl")


def backup_status_path() -> str:
    """backup_status.json sits next to the ledger (both live in CORE/Metadata
    on the host, /metadata in the API container)."""
    return str(Path(ledger_path()).parent / "backup_status.json")


def read_backup_status() -> Optional[dict]:
    try:
        with open(backup_status_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_restore_tests() -> Tuple[List[dict], bool]:
    """(restore_test lines oldest-first, ledger readable). Missing/unreadable
    ledger => ([], False) — the checks report that as unknown (manual), not
    fail: the reading simply does not exist on this install. Malformed /
    unrelated lines are skipped."""
    out: List[dict] = []
    try:
        with open(ledger_path(), "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(line, dict) and line.get("type") == "restore_test":
                    out.append(line)
    except OSError:
        return [], False
    return out, True
