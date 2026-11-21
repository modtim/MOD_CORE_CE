"""Startup fixture-audit pass — calibration-health-fixtures-and-timeouts §6.

design.md's Decision ("Fixture audit at startup, not at dispatch time",
Option A, chosen): at API startup, list every calibration-eligible NodeDef,
derive the sample-fixture object key the dispatcher would use for each
(tier, format) combination it could pick, and check whether that object
actually exists in the tenant's bucket. Log a one-line summary plus drop the
full miss list to a file an operator can act on. Never blocks startup and
never raises past its own boundary — a storage outage during the audit is a
miss, not a startup failure (spec: "The audit never blocks startup").

Key construction mirrors `routers.node_definitions._calibration_input_for_node`
exactly (first entry of `accepted_formats`, lowercased, dot-stripped,
`calibration-data/{tier}/sample.{ext}`) without importing that module — the
audit only needs to know the SAME rule, not share code with the (out-of-
scope-for-this-change) dispatcher module. A node with no `accepted_formats`
needs no fixture at all (the worker's executor short-circuits the download
sidecar on an empty source_file_location), so it is skipped rather than
counted as a miss.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import time
from typing import Callable, List, Optional, Tuple

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Mirrors services.calibration_workflow.SIZE_TIERS / the dispatcher's tier
# list. Kept as a local constant (not imported) so this read-only audit has
# no import-time dependency on the dispatch modules it is auditing.
SIZE_TIERS = ("small", "medium", "large")

# Eligible for calibration and therefore in-scope for the fixture audit.
# Mirrors calibration_coordinator's node-selection filter (excludes
# excluded_from_calibration and already-calibrated nodes).
_ELIGIBLE_STATUSES = ("uncalibrated", "stale", "calibrating")

DEFAULT_DUMP_DIR = "/tmp"

# Manifest key the fixture-migrator writes at the root of the shared
# read-only platform bucket `platform-calibration-data` (see
# DockerContainers/fixture-migrator/entrypoint.py). Its `files` map is
# `{tier}/sample.{ext}` -> {"sha256": ..., "size": ...}.
SHARED_BUCKET_MANIFEST = "manifest.json"

# One row per miss: (node_definition_id, node_name, bucket, tier, ext)
Miss = Tuple[int, str, str, str, str]


def _fixture_key(tier: str, ext: str) -> str:
    return f"calibration-data/{tier}/sample.{ext}"


def _bucket_for_node(node_def, db) -> str:
    """Tenant bucket the dispatcher would look in for this node's fixture.

    System nodes (`tenant_id IS NULL`) dispatch against the platform tenant
    — `services.calibration_dispatcher.auto_queue_for_profile` and
    `calibration_workflow.dispatch_calibration_run`'s root payload both use
    `tenant_id or 1` / "Platform tenant owns the system calibration matrix".
    Per-tenant node copies use their own tenant_id.

    WS4.Q17.12 — resolved through `get_tenant_bucket_name` (the same helper
    the dispatch/credential paths use) so a tenant with an ACTIVE BYO-S3
    connection is audited against ITS bucket, not the house default.
    """
    tenant_id = getattr(node_def, "tenant_id", None) or 1
    from utils.minio_client import get_tenant_bucket_name
    return get_tenant_bucket_name(tenant_id, db)


def node_input_format(node_def) -> Optional[str]:
    """First entry of accepted_formats, normalised — or None when the node
    takes no file input (no fixture required)."""
    formats = getattr(node_def, "accepted_formats", None)
    if not isinstance(formats, list) or not formats:
        return None
    first = formats[0]
    if not isinstance(first, str) or not first.strip():
        return None
    return first.strip().lstrip(".").lower()


def _object_exists(client, bucket: str, key: str) -> bool:
    client.stat_object(bucket, key)
    return True


def _object_sha256(client, bucket: str, key: str) -> Optional[str]:
    """sha256 of a stored object's bytes, or None when the object is absent.

    Mirrors fixture-migrator.remote_sha256: a single `get_object` in a
    try/except (NoSuchKey/S3Error both mean absent), then stream-hash the
    body in chunks so a large fixture never lands in memory whole.
    """
    try:
        resp = client.get_object(bucket, key)
    except Exception:  # noqa: BLE001 — any S3 error means "not usable"
        return None
    h = hashlib.sha256()
    with resp:
        for chunk in iter(lambda: resp.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _default_client(node_def):
    """Production storage-client factory — a MinioClient scoped to this
    node's tenant bucket. Imported lazily so unit tests can inject a stub
    via `client_factory` without needing utils.minio_client importable."""
    from utils.minio_client import MinioClient

    tenant_id = getattr(node_def, "tenant_id", None) or 1
    return MinioClient(tenant_id=tenant_id).client


def run_fixture_audit(
    db: Session,
    dump_dir: Optional[str] = None,
    client_factory: Optional[Callable] = None,
) -> dict:
    """One-shot pass over every calibration-eligible NodeDef.

    Returns a summary dict:
        {"checked": int, "present": int, "missing": int, "skipped": int,
         "misses": [Miss, ...], "dump_path": str | None}

    Never raises — any per-node or whole-pass failure (DB unreachable,
    storage unreachable) is logged and folded into the summary instead of
    propagating, so a caller wiring this into startup never needs its own
    try/except to keep the app booting.
    """
    summary = {
        "checked": 0, "present": 0, "missing": 0, "skipped": 0,
        "misses": [],  # type: List[Miss]
        "dump_path": None,
    }

    try:
        import models

        # Plain column rows, not ORM instances. Loading ~750 NodeDefinitions
        # into the session and then closing it detached them one by one, and
        # that teardown (weakref callbacks re-entering InstanceState attribute
        # writes) corrupted the heap on CPython 3.11 → API workers segfaulted
        # ~10s after every boot, which respawned them into the same crash.
        # Rows expose the same attributes (`.id`, `.name`, `.tenant_id`,
        # `.accepted_formats`) the audit reads, so nothing downstream changes.
        nd = models.NodeDefinition
        nodes = db.query(nd.id, nd.name, nd.tenant_id, nd.accepted_formats).filter(
            models.NodeDefinition.calibration_status.in_(_ELIGIBLE_STATUSES),
            models.NodeDefinition.excluded_from_calibration.is_(False),
        ).all()
    except Exception as e:  # noqa: BLE001 — audit must never block startup
        logger.warning(
            "[calibration_health] fixture audit could not list NodeDefinitions "
            "(non-fatal, skipping this pass): %s", e,
        )
        return summary

    for node_def in nodes:
        fmt = node_input_format(node_def)
        if fmt is None:
            summary["skipped"] += 1
            continue

        bucket = _bucket_for_node(node_def, db)
        try:
            client = (client_factory or _default_client)(node_def)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[calibration_health] fixture audit: could not build a storage "
                "client for node=%s tenant=%s bucket=%s (%d tier(s) counted as "
                "missing): %s",
                node_def.id, getattr(node_def, "tenant_id", None), bucket,
                len(SIZE_TIERS), e,
            )
            for tier in SIZE_TIERS:
                summary["checked"] += 1
                summary["missing"] += 1
                summary["misses"].append(
                    (node_def.id, node_def.name, bucket, tier, fmt))
            continue

        for tier in SIZE_TIERS:
            key = _fixture_key(tier, fmt)
            summary["checked"] += 1
            try:
                present = _object_exists(client, bucket, key)
            except Exception as e:
                logger.debug(
                    "[calibration_health] missing fixture: tenant=%s node=%s "
                    "tier=%s ext=%s (%s)",
                    bucket, node_def.id, tier, fmt, e,
                )
                present = False
            if present:
                summary["present"] += 1
                logger.debug(
                    "[calibration_health] fixture present: tenant=%s node=%s "
                    "tier=%s ext=%s",
                    bucket, node_def.id, tier, fmt,
                )
            else:
                summary["missing"] += 1
                summary["misses"].append(
                    (node_def.id, node_def.name, bucket, tier, fmt))

    summary["dump_path"] = _write_dump(summary, dump_dir)

    logger.info(
        "[calibration_health] fixture audit: %d of %d present, %d missing "
        "(%d node(s) skipped, no accepted_formats)%s",
        summary["present"], summary["checked"], summary["missing"],
        summary["skipped"],
        f" — see {summary['dump_path']} for full list" if summary["dump_path"] else "",
    )
    return summary


def _write_dump(summary: dict, dump_dir: Optional[str]) -> Optional[str]:
    """Drop the full miss list to a file an operator can act on. Only
    written when there is at least one miss — an all-green pass leaves no
    file behind. Never raises: a write failure is logged and the pass
    proceeds with dump_path=None."""
    if not summary["misses"]:
        return None
    directory = dump_dir or os.getenv("CALIBRATION_HEALTH_DUMP_DIR", DEFAULT_DUMP_DIR)
    ts = int(time.time())
    path = os.path.join(directory, f"calibration_health_{ts}.txt")
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w") as f:
            f.write(
                f"calibration fixture audit — {summary['missing']} missing "
                f"of {summary['checked']} checked\n"
            )
            f.write(
                f"generated: "
                f"{datetime.datetime.now(datetime.timezone.utc).isoformat()}\n\n"
            )
            for node_id, name, bucket, tier, ext in summary["misses"]:
                f.write(
                    f"node_definition_id={node_id} name={name!r} bucket={bucket} "
                    f"tier={tier} ext={ext} key={_fixture_key(tier, ext)}\n"
                )
        return path
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[calibration_health] could not write miss dump to %s: %s", path, e)
        return None


def run_fixture_audit_startup(db_session_factory: Optional[Callable] = None) -> None:
    """Entry point wired into API startup (main.py's `_startup_tasks`), run
    in its own thread so a slow/unreachable storage backend cannot delay
    app boot. Swallows everything — a fixture-audit bug must never take the
    API down with it.
    """
    try:
        if db_session_factory is None:
            import database
            db_session_factory = database.SessionLocal
        db = db_session_factory()
        try:
            run_fixture_audit(db)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 — startup must never fail on this
        logger.warning(
            "[calibration_health] fixture audit failed at startup (non-fatal): %s",
            e,
        )


def audit_shared_bucket(client=None, bucket="platform-calibration-data") -> List[str]:
    """Audit the shared read-only platform calibration bucket against its
    `manifest.json`.

    Loads `manifest.json` from the bucket root and, for every entry in its
    `files` map (`{tier}/sample.{ext}` -> {"sha256": ..., "size": ...}),
    checks that the object exists and its sha256 matches the recorded
    `sha256`. Returns the object keys that are missing or whose content no
    longer matches — an empty list means the bucket is healthy. A missing or
    unreadable `manifest.json` is itself reported as `["manifest.json"]`.

    `client` is the S3/Minio client (injected for tests, only `get_object` is
    exercised); when None a platform-tenant client is built via
    `_default_client` (tenant 1 — the shared bucket is platform-owned).
    """
    if client is None:
        client = _default_client(None)

    try:
        resp = client.get_object(bucket, SHARED_BUCKET_MANIFEST)
    except Exception:  # noqa: BLE001 — NoSuchKey / storage outage
        return [SHARED_BUCKET_MANIFEST]
    with resp:
        manifest_bytes = resp.read()
    try:
        files = json.loads(manifest_bytes)["files"]
    except Exception:  # noqa: BLE001 — corrupt/missing "files" is unusable
        return [SHARED_BUCKET_MANIFEST]
    if not isinstance(files, dict):
        return [SHARED_BUCKET_MANIFEST]

    bad: List[str] = []
    for key, meta in files.items():
        expected = meta.get("sha256") if isinstance(meta, dict) else None
        actual = _object_sha256(client, bucket, key)
        if actual is None or actual != expected:
            bad.append(key)
    return bad
