"""FX.74 p3b [B] — weekly refresh of the OFAC SDN list used by
services/sanctions_screening.py (p3a). No-op unless sanctions.refresh_enabled.
Fetches sanctions.refresh_url (https only, safe_http GET) or, air-gapped,
copies newer sdn.csv/alt.csv in from sanctions.refresh_drop_dir. Candidates
are staged in temp files next to sanctions.sdn_path, validated (load_sdn must
yield >= sanctions.refresh_min_entries, default 1000) and only then os.replace
'd, so a short/broken list never touches the live file. sha256 + fetched_at go
into a sidecar JSON; one audit row per run (p3a's AuditAction, metadata
{source, entries, ok, error?}); never raises out."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
try:
    from services import sanctions_screening
except (ImportError, ModuleNotFoundError):
    sanctions_screening = None
from utils import safe_http

logger = logging.getLogger(__name__)
REFRESH_ENABLED_KEY = "sanctions.refresh_enabled"
REFRESH_URL_KEY = "sanctions.refresh_url"
REFRESH_DROP_DIR_KEY = "sanctions.refresh_drop_dir"
REFRESH_MIN_ENTRIES_KEY = "sanctions.refresh_min_entries"
AUDIT_ACTION = sanctions_screening.AUDIT_ACTION if sanctions_screening else None
DEFAULT_URL = "https://www.treasury.gov/ofac/downloads/sdn.csv"
DEFAULT_MIN_ENTRIES = 1000
LIST_NAMES = ("sdn.csv", "alt.csv")
get_setting = sanctions_screening.get_setting if sanctions_screening else None


def _stage(db, fetch) -> dict:
    """{staged: {name: tmp}, source, target}; staged empty = nothing newer."""
    target = get_setting(db, sanctions_screening.SDN_PATH_KEY,
                         sanctions_screening.DEFAULT_SDN_PATH) \
        or sanctions_screening.DEFAULT_SDN_PATH
    directory = os.path.dirname(target) or "."
    staged: dict[str, str] = {}
    drop = str(get_setting(db, REFRESH_DROP_DIR_KEY, "") or "")
    if drop:
        for name in LIST_NAMES:
            src = os.path.join(drop, name)
            live = os.path.join(directory, name)
            if not os.path.exists(src) or (
                    os.path.exists(live)
                    and os.path.getmtime(src) <= os.path.getmtime(live)
                    and open(src, "rb").read() == open(live, "rb").read()):
                continue
            tmp = os.path.join(directory, name + ".refresh-tmp")
            with open(src, "rb") as fh, open(tmp, "wb") as out:
                out.write(fh.read())
            staged[name] = tmp
        return {"staged": staged, "source": f"drop:{drop}", "target": target}
    url = str(get_setting(db, REFRESH_URL_KEY, DEFAULT_URL) or DEFAULT_URL)
    if not url.lower().startswith("https://"):
        raise ValueError("sanctions.refresh_url must be https")
    data = fetch(url) if fetch else safe_http.get(
        url, timeout=60, stream=True, max_bytes=64 * 1024 * 1024).content
    tmp = os.path.join(directory, "sdn.csv.refresh-tmp")
    with open(tmp, "wb") as fh:
        fh.write(data if isinstance(data, (bytes, bytearray))
                 else str(data).encode("utf-8"))
    return {"staged": {"sdn.csv": tmp}, "source": url, "target": target}


def refresh_sdn(db, fetch=None) -> dict:
    """One refresh run. {ok, status, source, entries, sha256?, error?}."""
    if sanctions_screening is None:
        logger.info("FX.74 p3b: sanctions_screening unavailable (CE) — skipping")
        return {"ok": True, "status": "skipped", "reason": "sanctions_screening_unavailable"}
    try:
        outcome = _refresh(db, fetch)
    except Exception as exc:  # noqa: BLE001 — never raise out
        logger.warning("FX.74 p3b: sdn refresh failed: %s", exc)
        outcome = {"ok": False, "status": "error", "source": None,
                   "entries": 0, "error": str(exc)[:300]}
    if outcome.get("status") != "skipped":
        _write_audit(db, outcome)
    return outcome


def _refresh(db, fetch) -> dict:
    if not bool(get_setting(db, REFRESH_ENABLED_KEY, False)):
        return {"ok": True, "status": "skipped", "reason": "disabled"}
    raw_min = get_setting(db, REFRESH_MIN_ENTRIES_KEY, DEFAULT_MIN_ENTRIES)
    minimum = int(raw_min) if isinstance(raw_min, int) else DEFAULT_MIN_ENTRIES
    plan = _stage(db, fetch)
    target, directory = plan["target"], os.path.dirname(plan["target"]) or "."
    staged = plan["staged"]
    if not staged:
        return {"ok": True, "status": "skipped", "reason": "no_newer_files",
                "source": plan["source"]}
    alt = staged.get("alt.csv", os.path.join(directory, "alt.csv"))
    entries = len(sanctions_screening.load_sdn(
        staged.get("sdn.csv", target), alt if os.path.exists(alt) else None))
    if entries < minimum:
        for tmp in staged.values():
            os.remove(tmp)
        return {"ok": False, "status": "error", "source": plan["source"],
                "entries": entries,
                "error": f"list too small ({entries} < {minimum} entries)"}
    for name, tmp in staged.items():
        os.replace(tmp, os.path.join(directory, name))
    digest = hashlib.sha256(open(target, "rb").read()).hexdigest()
    with open(target + ".meta.json", "w", encoding="utf-8") as fh:
        json.dump({"sha256": digest, "fetched_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "entries": entries,
            "source": plan["source"]}, fh)
    return {"ok": True, "status": "refreshed", "source": plan["source"],
            "entries": entries, "sha256": digest}


def _write_audit(db, outcome: dict) -> None:
    try:
        from audit import log_security_event
        meta = {"kind": "sanctions_sdn_refresh",
                "source": outcome.get("source"),
                "entries": outcome.get("entries", 0),
                "ok": bool(outcome.get("ok")),
                **({"error": outcome["error"]} if outcome.get("error") else {})}
        log_security_event(db=db, action=AUDIT_ACTION, metadata=meta)
    except Exception as exc:  # noqa: BLE001 — audit is best effort
        logger.warning("FX.74 p3b: audit row failed: %s", exc)
