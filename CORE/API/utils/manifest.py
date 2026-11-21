"""Node manifest — signed sidecar JSON per built image.

Emitted by imagebuilder at end of a successful docker push. Lives at
`s3://tenant-{id}-files/node-manifests/{image_name}-{sha256_prefix}.json`.
Signed with the same HMAC key the audit module uses (`AUDIT_HMAC_KEY`).

Enables:
  - Portability: export/import a tenant's node set (manifest + image
    layers) between MOD instances.
  - Auditability: independent verification against the live registry
    and the DB; catches tampering or silent retagging.
  - Rescan traceability: did this quarter's rescan use the same base-
    image digest the node was originally approved on?
  - Attestation chain: admission verdict, classifier version, builder
    commit all baked into the manifest.

This module is the **data shape + signer**. The actual emit hook lives
in the imagebuilder container (CORE/DockerContainers/ImageBuilder/build.py)
which calls sign_manifest() and uploads to MinIO at end of push.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


MANIFEST_VERSION = 1


def _hmac_key() -> bytes:
    """Read the HMAC key from env. Same key used by the audit module so
    operators manage one secret, not two."""
    return os.getenv("AUDIT_HMAC_KEY", "").encode()


def _canonical_json(obj: Any) -> str:
    """Deterministic JSON serialization so the signature is stable across
    Python runs. sort_keys + no whitespace + UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass
class ImageIdentity:
    ref: str
    digest: str
    size_bytes: int = 0
    base_image: str = ""
    base_digest: str = ""


@dataclass
class RuntimeContract:
    input_type: str = "image"
    output_type: str = "image"
    process_type: str = "CPU"
    writable_paths: list[str] = field(default_factory=list)
    allowed_egress: list[dict] = field(default_factory=list)
    env_vars: list[dict] = field(default_factory=list)


@dataclass
class SecurityAttestation:
    scan_status: str = "pending_scan"
    scan_summary: dict = field(default_factory=dict)
    scan_exceptions: list = field(default_factory=list)
    scanned_at: Optional[str] = None
    classifier_version: str = "1.0"


@dataclass
class BuildProvenance:
    builder_image: str = ""
    builder_commit: str = ""
    dockerfile_sha256: str = ""
    admission_verdict: str = "passed"
    llm_retries: int = 0
    base_detector_version: str = "1.0"


@dataclass
class NodeManifest:
    node_name: str
    version: int
    tenant_id: Optional[int]
    created_by_user_id: Optional[int]
    image: ImageIdentity
    runtime_contract: RuntimeContract
    security: SecurityAttestation
    build: BuildProvenance
    manifest_version: int = MANIFEST_VERSION
    created_at: str = ""
    signature: str = ""

    def to_unsigned_dict(self) -> dict:
        """The dict we sign. Excludes the signature field itself."""
        d = asdict(self)
        d.pop("signature", None)
        return d

    def to_signed_dict(self) -> dict:
        d = asdict(self)
        return d


def build_manifest(
    *,
    node_name: str,
    version: int,
    tenant_id: Optional[int],
    created_by_user_id: Optional[int],
    image_ref: str,
    image_digest: str,
    image_size_bytes: int = 0,
    base_image: str = "",
    base_digest: str = "",
    runtime_contract: Optional[RuntimeContract] = None,
    security: Optional[SecurityAttestation] = None,
    build: Optional[BuildProvenance] = None,
) -> NodeManifest:
    """Assemble an unsigned manifest. Call sign_manifest() before emit."""
    return NodeManifest(
        node_name=node_name,
        version=version,
        tenant_id=tenant_id,
        created_by_user_id=created_by_user_id,
        image=ImageIdentity(
            ref=image_ref,
            digest=image_digest,
            size_bytes=image_size_bytes,
            base_image=base_image,
            base_digest=base_digest,
        ),
        runtime_contract=runtime_contract or RuntimeContract(),
        security=security or SecurityAttestation(),
        build=build or BuildProvenance(),
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


def sign_manifest(manifest: NodeManifest) -> NodeManifest:
    """Compute HMAC-SHA256 over the canonical JSON of the unsigned
    manifest and set manifest.signature. Idempotent — calling twice on
    the same manifest produces the same signature (signature field is
    excluded from the input)."""
    key = _hmac_key()
    if not key:
        logger.warning(
            "AUDIT_HMAC_KEY not set; manifest will be emitted unsigned. "
            "Verification will fail. Configure the key for prod."
        )
        manifest.signature = ""
        return manifest
    payload = _canonical_json(manifest.to_unsigned_dict()).encode()
    manifest.signature = "hmac-sha256:" + _hmac.new(key, payload, hashlib.sha256).hexdigest()
    return manifest


def verify_manifest_signature(manifest_dict: dict) -> tuple[bool, str]:
    """Return (ok, reason). ok=True iff the signature field matches an
    HMAC of the rest of the manifest with the current AUDIT_HMAC_KEY."""
    key = _hmac_key()
    if not key:
        return False, "AUDIT_HMAC_KEY not configured on this server"
    sig = manifest_dict.get("signature") or ""
    if not sig.startswith("hmac-sha256:"):
        return False, f"Unsupported signature scheme: {sig[:20]!r}"
    expected_hex = sig[len("hmac-sha256:"):]

    unsigned = {k: v for k, v in manifest_dict.items() if k != "signature"}
    payload = _canonical_json(unsigned).encode()
    computed_hex = _hmac.new(key, payload, hashlib.sha256).hexdigest()

    if not _hmac.compare_digest(expected_hex, computed_hex):
        return False, "Signature mismatch — manifest was modified or key differs"
    return True, "ok"


def manifest_storage_key(tenant_id: Optional[int], image_name: str, image_digest: str) -> str:
    """MinIO object key for a given manifest. Digest short-prefix in the
    filename so we can keep old manifests around for audit without
    collisions."""
    short = (image_digest or "").replace("sha256:", "")[:12] or "unknown"
    safe_name = (image_name or "unknown").replace("/", "-").replace(":", "-")
    tenant = tenant_id if tenant_id is not None else "system"
    return f"node-manifests/{safe_name}-{short}.json"


def manifest_bucket(tenant_id: Optional[int], db=None) -> str:
    """Per-tenant MinIO bucket. System nodes land in the platform bucket
    (`mod-core-files`) rather than a tenant one.

    With a ``db`` session, routes through
    ``utils.minio_client.get_tenant_bucket_name`` so a tenant with an
    active BYO-S3 ``StorageConnection`` gets its own bucket (read/write
    consistency with the imagebuilder's emit path when it is given
    BYO credentials); without one (or on any resolution failure) it
    falls back to the house default ``tenant-{id}-files``."""
    if tenant_id is None:
        return "mod-core-files"
    from utils.minio_client import get_tenant_bucket_name
    return get_tenant_bucket_name(tenant_id, db)
