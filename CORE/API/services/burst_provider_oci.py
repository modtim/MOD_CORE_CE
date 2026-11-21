"""GH.7e — Oracle Cloud Infrastructure (OCI) burst provider (launch-
completion-megaplan group H, decomp/gh-burst.md), the fifth real cloud
provider for burst-into-customer-cloud (after EC2, Azure, Lambda; GCP is a
sibling in-progress adapter).

Driver choice: pinned `apache-libcloud>=3.8` (requirements.txt) has NO
usable Oracle Cloud Infrastructure compute driver — libcloud's provider
list has never carried a modern-OCI entry, and OCI's own request-signing
scheme (RSA-SHA256 over a canonical string built from a specific per-method
header set — quite different from AWS SigV4, Azure AD bearer tokens, or
GCE's service-account JWT) is not something libcloud implements for it.
This adapter is therefore built DIRECTLY on the official `oci` Python SDK
(``pip install oci``, added to requirements.txt as `oci>=2.120` right after
the apache-libcloud line — NOT installed in this environment, and that is
fine: see the lazy-import note below), the same shape as
`services/burst_provider_lambda.py`'s direct-REST Lambda adapter — i.e. it
implements `BurstProvider` directly rather than subclassing
`LibcloudBurstProvider`.

UNLIKE the Lambda adapter, this is explicitly NOT hand-rolled over raw
REST: OCI request signing is genuinely complex and this module never
constructs a signature itself. The official SDK's `oci.core.ComputeClient`
(built from an `oci.config`-shaped dict) owns all of the signing and HTTP
transport; this module only calls its methods (`launch_instance`,
`list_instances`, `terminate_instance`, `list_shapes`) and builds its
request-model objects (`oci.core.models.LaunchInstanceDetails`,
`InstanceSourceViaImageDetails`).

The `oci` import is LAZY — confined to `_oci_module()` — mirroring how
`services/burst_provider_ec2.py`'s `_ec2_driver_class()` lazily imports
libcloud: the running API image does not have `oci` installed, and a
top-level import here would crash API startup (this module is imported at
startup by `services/burst_provider.py`'s trailing registration import).
Tests inject a fake `oci` module through this exact seam
(`monkeypatch.setattr(burst_provider_oci, "_oci_module", lambda: fake_oci)`)
and never need the real package.

Credentials (parsed tenant secret dict, see
`burst_accounts._creds_from_secret`) map onto the six OCI config fields
`oci.config` needs:

  * tenancy OCID        — `tenancy` (or `tenancy_ocid`)
  * user OCID           — `user` (or `user_ocid`)
  * key fingerprint     — `fingerprint`
  * private key (PEM)   — `key_content` (or `private_key`) — the PEM
    CONTENTS themselves (matches the SDK config dict's `key_content` field;
    this module never touches a key FILE path)
  * region              — `region` (default `DEFAULT_REGION`)
  * compartment OCID     — `compartment_id` (or `compartment`) — REQUIRED:
    every OCI compute call (`list_instances`, `launch_instance`,
    `list_shapes`) is compartment-scoped; a clear `ValueError` is raised
    when it is missing.
  * availability domain — `availability_domain` (or `ad`) — REQUIRED: OCI
    instance launches are AD-scoped, unlike EC2/Azure/GCE where the region/
    zone alone is enough; a clear `ValueError` is raised when it is missing.

`fingerprint` and the PEM (`key_content`) are the two credential values
that can appear inside an SDK error string, so both go into
`redact_values` and are stripped from every error this module returns.

GPU shapes (approx, list price — for ranking "cheapest fitting" only, never
for billing; see `pick_instance_type`):

  * `VM.GPU.A10.1`      — 1x A10 24GB           ~$2.00/h
  * `VM.GPU.A10.2`      — 2x A10 24GB           ~$4.00/h
  * `BM.GPU.A100-v2.8`  — 8x A100 80GB (bare metal) ~$32.00/h
  * `BM.GPU.H100.8`     — 8x H100 80GB (bare metal) ~$80.00/h

Tags: OCI freeform tags (`freeform_tags`, a flat `str -> str` dict on the
`Instance` model, settable at `launch_instance` time and inspectable on
every `list_instances`/`get_instance` response) carry the MOD tag
NATIVELY — no sanitisation or name-marker workaround is needed (unlike
GCE's label charset or Lambda's total absence of tags). `list_tagged`
filters `list_instances()` results on the freeform tag; `teardown` refuses
(raises `PermissionError`, touching nothing for that id) any instance whose
freeform tags do not carry `mod-burst=mod-core`; a refused instance is
never terminated. Termination calls `terminate_instance(id,
preserve_boot_volume=False)` — MOD-launched instances own their boot
volume and it must not be orphaned as a silent, un-reconciled cost.

*** SELF-DESTRUCT WARNING ***
The `arm_self_destruct` shutdown line (`shutdown -h +<max_lifetime_min>`,
shared verbatim with the EC2/Azure/GCP/Lambda adapters) is spliced into the
instance's cloud-init user-data and placed in `metadata["user_data"]`,
BASE64-ENCODED as OCI's launch API requires (OCI decodes `metadata
["user_data"]` as base64 before feeding it to cloud-init on first boot —
an un-encoded string is rejected/garbled). Running that line only STOPS
the instance (the OCI "STOPPED" lifecycle state) — it does NOT terminate
it, and critically, bare-metal GPU shapes (`BM.GPU.*`) continue to bill
while STOPPED, because the entire physical host is reserved for that
tenant regardless of guest power state. A VM GPU shape at least stops
metering; a bare-metal one does not. That means the in-guest shutdown is,
at best, a "stop doing work" safety net — the reconciler's MOD-tag sweep
(`services/burst_reconciler.py`, `provider.list_tagged` + `provider.
teardown`, which calls `terminate_instance`) is the ONLY call that actually
stops OCI from billing an orphaned burst instance, exactly as it is the
real reaper for the Lambda adapter's un-terminated orphans.

Capacity: OCI signals "no capacity for this shape in this AD" as an
`oci.exceptions.ServiceError` whose `code`/`message` mentions "capacity" —
there is no single stable error code across shapes/regions/AD generations,
so (mirroring `burst_provider_lambda._is_capacity_error`'s substring
approach, for the same reason: surviving the exact wording being slightly
off) `_is_capacity_error` matches on the substring "capacity" in either
field. `provision()` raises `BurstCapacityUnavailable` on a match —
imported from `services.burst_provider_lambda` (no import cycle: `services/
burst_provider.py`'s trailing registration imports load `burst_provider_ec2`
before `burst_provider_lambda` before this module, so by the time this
module is imported, `burst_provider_lambda` is already fully loaded in
`sys.modules` and this is a plain forward reference, not a cycle). Callers
should treat it exactly as documented on the Lambda adapter: transient,
supply-side unavailability, not an account-invalidating error.

`probe()` is strictly read-only: `list_tagged` (via `list_instances`) plus
`list_shapes` (compartment-scoped shape catalogue) — no writes. Any
exception leaves `ok=False` with the error redacted.
"""
from __future__ import annotations

import base64
import itertools
from typing import Any

from services.burst_provider import (
    MOD_TAG_KEY,
    has_mod_tag,
    mod_tag_value,
    register_provider,
)
from services.burst_provider_ec2 import arm_self_destruct
from services.burst_provider_lambda import BurstCapacityUnavailable

DEFAULT_REGION = "us-ashburn-1"
# A burst instance that never enrolls still has a hard ceiling: default
# 12 h, same concept as the EC2/Azure/GCP/Lambda adapters (see the
# SELF-DESTRUCT WARNING above for why this is belt-and-braces on OCI, not a
# substitute for the reconciler's terminate sweep).
DEFAULT_MAX_LIFETIME_MIN = 720

# ============================================================================
# GPU size map — MOD VRAM need -> OCI shape name
# ============================================================================
# `vram_gb` is the per-GPU memory; `count` the number of GPUs, so a shape's
# total VRAM is count * vram_gb. `price` is an APPROX, LIST PRICE $/hour
# figure used ONLY to order "cheapest fitting" — never for billing.
GPU_SIZE_MAP: dict[str, dict[str, Any]] = {
    "VM.GPU.A10.1":     {"gpu": "A10",  "count": 1, "vram_gb": 24, "price": 2.00},
    "VM.GPU.A10.2":     {"gpu": "A10",  "count": 2, "vram_gb": 24, "price": 4.00},
    "BM.GPU.A100-v2.8": {"gpu": "A100", "count": 8, "vram_gb": 80, "price": 32.00},
    "BM.GPU.H100.8":    {"gpu": "H100", "count": 8, "vram_gb": 80, "price": 80.00},
}


def total_vram_gb(instance_type: str) -> int:
    """Total VRAM (count * per-GPU) for a mapped OCI shape; 0 if unknown."""
    spec = GPU_SIZE_MAP.get(instance_type)
    return spec["count"] * spec["vram_gb"] if spec else 0


def pick_instance_type(
    min_vram_gb: int | float,
    allowed_instance_types: list[str] | None = None,
) -> str:
    """Pick the CHEAPEST mapped OCI shape whose total VRAM is at least
    `min_vram_gb`.

    Same semantics as the EC2/Azure/GCP/Lambda `pick_instance_type`:
    `allowed_instance_types` (the account's `limits.allowed_instance_types`)
    restricts the choice when provided. Raises `ValueError` with a clear
    message when the need is non-positive or no mapped shape fits.
    """
    try:
        need_val = float(min_vram_gb)
    except (TypeError, ValueError):
        raise ValueError(f"min_vram_gb must be a number, got {min_vram_gb!r}")
    if need_val <= 0:
        raise ValueError(f"min_vram_gb must be > 0, got {min_vram_gb!r}")

    allowed = (
        set(allowed_instance_types)
        if allowed_instance_types is not None
        else None
    )
    candidates: list[tuple[float, str]] = []
    for itype, spec in GPU_SIZE_MAP.items():
        if allowed is not None and itype not in allowed:
            continue
        if spec["count"] * spec["vram_gb"] >= need_val:
            candidates.append((float(spec["price"]), itype))
    if not candidates:
        where = (
            f" within allowed {sorted(allowed)}" if allowed is not None else ""
        )
        raise ValueError(
            f"no OCI GPU shape in the size map provides >= {min_vram_gb} "
            f"GB VRAM{where}; mapped shapes: {sorted(GPU_SIZE_MAP)}"
        )
    # Cheapest first; break ties by name for determinism.
    candidates.sort(key=lambda p: (p[0], p[1]))
    return candidates[0][1]


# ============================================================================
# Node-dict helper
# ============================================================================

def _node_dict(instance: Any) -> dict[str, Any]:
    """Coerce an `oci.core.models.Instance` (or anything with the same
    attributes) into the plain-dict shape every BurstProvider returns.

    OCI does not carry IP addresses on the `Instance` model itself (they
    live on the attached VNIC, fetched via a separate `list_vnic_
    attachments` + `get_vnic` round trip) — deliberately NOT fetched here
    to keep this adapter's read surface small; burst bookkeeping only ever
    needs id/state/tags to drive `list_tagged`/`teardown`. `public_ip`/
    `private_ip` are left `None`, same "unknown, not guessed" spirit as the
    Lambda adapter's `available: None` on `quote()`.
    """
    tags = dict(getattr(instance, "freeform_tags", None) or {})
    return {
        "id": str(getattr(instance, "id", "") or ""),
        "state": str(getattr(instance, "lifecycle_state", "") or ""),
        "public_ip": None,
        "private_ip": None,
        "tags": tags,
    }


# ============================================================================
# OciBurstProvider
# ============================================================================

class OciBurstProvider:
    """BurstProvider for Oracle Cloud Infrastructure, implemented directly
    over the official `oci` Python SDK's `oci.core.ComputeClient` (see the
    module docstring for why libcloud is not used and why this is not
    hand-rolled REST).

    `compute_client` is an `oci.core.ComputeClient` (or, in tests, a
    hand-written duck-typed stand-in exposing `launch_instance`,
    `list_instances`, `terminate_instance`, `list_shapes`) built by
    `_oci_factory` from the tenant's parsed credentials — this class never
    imports `oci` itself; only `provision()` needs the `oci.core.models`
    request types, fetched lazily via `_oci_module()`.
    """

    def __init__(
        self,
        compute_client: Any,
        *,
        compartment_id: str,
        availability_domain: str,
        region: str = DEFAULT_REGION,
        image_id: str = "mod-burst-image",
        max_lifetime_min: int = DEFAULT_MAX_LIFETIME_MIN,
        redact_values: tuple[str, ...] | list[str] = (),
    ):
        if not compartment_id:
            raise ValueError(
                "oci burst requires a compartment_id: every OCI compute "
                "call is compartment-scoped — set `compartment_id` (or "
                "`compartment`) on the account"
            )
        if not availability_domain:
            raise ValueError(
                "oci burst requires an availability_domain: OCI instance "
                "launches are AD-scoped — set `availability_domain` (or "
                "`ad`) on the account"
            )
        self.compute_client = compute_client
        self.compartment_id = compartment_id
        self.availability_domain = availability_domain
        self.region = region
        self.image_id = image_id
        self.max_lifetime_min = int(max_lifetime_min)
        # Values to strip from any error string before it is persisted/logged.
        self._redact_values = [v for v in redact_values if v]
        self._counter = itertools.count(1)

    def _redact(self, message: Any) -> str:
        s = str(message)
        for v in self._redact_values:
            if v:
                s = s.replace(v, "[REDACTED]")
        return s

    # -- reads ---------------------------------------------------------------

    def _list_instances(self) -> list[Any]:
        resp = self.compute_client.list_instances(
            compartment_id=self.compartment_id
        )
        items = getattr(resp, "data", None)
        return list(items) if items else []

    def _instances_by_id(self) -> dict[str, Any]:
        return {
            str(getattr(item, "id", "") or ""): item
            for item in self._list_instances()
        }

    # -- BurstProvider protocol -----------------------------------------------

    def quote(self, instance_type: str, n: int, region: str) -> dict[str, Any]:
        # No network call: static approx-price map only, same spirit as the
        # Lambda adapter's `quote()`. `available` is unknown without a live
        # capacity check, so it is left None rather than guessed.
        spec = GPU_SIZE_MAP.get(instance_type)
        price = float(spec["price"]) if spec else None
        return {
            "provider": "oci",
            "instance_type": instance_type,
            "n": n,
            "region": region,
            "available": None,
            "price_per_hour": price,
            "estimated_cost_per_hour": (
                round(price * n, 6) if price is not None else None
            ),
        }

    @staticmethod
    def _is_capacity_error(exc: Exception) -> bool:
        """True if `exc` (an `oci.exceptions.ServiceError`, or anything
        duck-typed the same way in tests) is OCI's "no capacity for this
        shape/AD" rejection. There is no single stable code across shapes/
        regions, so — mirroring `burst_provider_lambda._is_capacity_error`
        — this matches on the substring "capacity" in either `code` or
        `message`, so it survives the exact wording being slightly off."""
        code = str(getattr(exc, "code", "") or "").lower()
        message = str(getattr(exc, "message", "") or str(exc)).lower()
        return "capacity" in code or "capacity" in message

    def provision(
        self, n: int, instance_type: str, user_data: str, tags: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if not has_mod_tag(tags):
            raise ValueError(
                "provision() requires the MOD tag so teardown/list_tagged "
                "can find these nodes and nothing else"
            )
        oci = _oci_module()
        # Arm the hard max-lifetime self-destruct (see the SELF-DESTRUCT
        # WARNING in the module docstring for why this alone is NOT enough
        # on OCI), then base64-encode: OCI decodes metadata["user_data"] as
        # base64 before handing it to cloud-init on first boot.
        armed = arm_self_destruct(user_data, self.max_lifetime_min)
        user_data_b64 = base64.b64encode(armed.encode("utf-8")).decode("ascii")
        out = []
        for _ in range(n):
            details = oci.core.models.LaunchInstanceDetails(
                compartment_id=self.compartment_id,
                availability_domain=self.availability_domain,
                shape=instance_type,
                display_name=f"mod-burst-{next(self._counter)}",
                freeform_tags=dict(tags),
                metadata={"user_data": user_data_b64},
                source_details=oci.core.models.InstanceSourceViaImageDetails(
                    image_id=self.image_id
                ),
            )
            try:
                resp = self.compute_client.launch_instance(details)
            except Exception as e:  # noqa: BLE001 — narrowed by the check below
                if self._is_capacity_error(e):
                    raise BurstCapacityUnavailable(self._redact(
                        f"oci has no available capacity for shape "
                        f"{instance_type} in {self.availability_domain}: {e}"
                    )) from e
                raise
            out.append(_node_dict(resp.data))
        return out

    def status(self, node_ids: list[str]) -> list[dict[str, Any]]:
        by_id = self._instances_by_id()
        return [_node_dict(by_id[i]) for i in node_ids if i in by_id]

    def teardown(self, node_ids: list[str]) -> list[dict[str, Any]]:
        """Destroy MOD-tagged OCI instances ONLY. An instance whose
        freeform tags do not carry the MOD tag is refused — this is the
        guard that keeps burst from ever destroying a customer's non-MOD
        instance, even if MOD's own bookkeeping is wrong. Termination is
        `terminate_instance(id, preserve_boot_volume=False)` — the boot
        volume is MOD's own, created for this instance, and must not be
        left behind as an unreconciled cost."""
        by_id = self._instances_by_id()
        out = []
        for i in node_ids:
            item = by_id.get(i)
            if item is None:
                continue
            d = _node_dict(item)
            if not has_mod_tag(d["tags"]):
                raise PermissionError(
                    f"refusing to tear down oci instance {i}: missing MOD tag"
                )
            self.compute_client.terminate_instance(
                i, preserve_boot_volume=False
            )
            d["state"] = "terminated"
            out.append(d)
        return out

    def list_tagged(self, tags: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for item in self._list_instances():
            d = _node_dict(item)
            if not has_mod_tag(d["tags"]):
                continue
            if all(d["tags"].get(k) == v for k, v in tags.items()
                   if k != MOD_TAG_KEY):
                out.append(d)
        return out

    # -- read-only credential probe ------------------------------------------

    def probe(self) -> dict[str, Any]:
        """Read-only credential/capability probe.

        Makes only read calls — `list_instances` (filtered to the MOD tag
        via `list_tagged`) + `list_shapes` for the compartment — and
        returns ``{ok, region, compartment_id, visible_tagged, error}``.
        On ANY exception `ok` is False and the error string has the
        fingerprint/PEM redacted out."""
        result: dict[str, Any] = {
            "ok": False,
            "region": self.region,
            "compartment_id": self.compartment_id,
            "visible_tagged": None,
            "error": None,
        }
        try:
            tagged = self.list_tagged({MOD_TAG_KEY: mod_tag_value()})
            # Confirm the compartment's shape catalogue is reachable
            # (read-only).
            shapes = self.compute_client.list_shapes(
                compartment_id=self.compartment_id
            )
            result["visible_tagged"] = len(tagged)
            result["ok"] = True
            # (shape catalogue reachable; contents intentionally not
            # surfaced)
            del shapes
        except Exception as e:  # noqa: BLE001 — probe must never raise
            result["error"] = self._redact(e)
        return result


# ============================================================================
# Lazy SDK import + factory + registration
# ============================================================================

def _oci_module() -> Any:
    """LAZY import of the official `oci` SDK — the ONLY place this module
    touches it, exactly like `services/burst_provider_ec2.py`'s
    `_ec2_driver_class()` lazily importing libcloud. Kept as a single
    function so tests can monkeypatch it with a fake stand-in and never
    require the real `oci` package (which is NOT installed in this
    environment)."""
    import oci  # noqa: PLC0415 (deliberately lazy)

    return oci


def _oci_factory(**cfg: Any) -> OciBurstProvider:
    """Build an OciBurstProvider from credential kwargs (see the module
    docstring for the credential-key mapping).

    `cfg` is the parsed tenant secret (see
    `burst_accounts._creds_from_secret`) plus optional account config.
    `oci.config` (the SDK's plain config dict) is built from `tenancy`,
    `user`, `fingerprint`, `key_content`, `region`; `oci.core.
    ComputeClient(config)` signs and transports every call — this module
    never signs a request itself.
    """
    oci = _oci_module()
    tenancy = cfg.get("tenancy") or cfg.get("tenancy_ocid")
    user = cfg.get("user") or cfg.get("user_ocid")
    fingerprint = cfg.get("fingerprint")
    key_content = cfg.get("key_content") or cfg.get("private_key")
    region = cfg.get("region", DEFAULT_REGION)
    compartment_id = cfg.get("compartment_id") or cfg.get("compartment")
    availability_domain = cfg.get("availability_domain") or cfg.get("ad")

    config = {
        "tenancy": tenancy,
        "user": user,
        "fingerprint": fingerprint,
        "key_content": key_content,
        "region": region,
    }
    compute_client = oci.core.ComputeClient(config)
    return OciBurstProvider(
        compute_client,
        compartment_id=compartment_id,
        availability_domain=availability_domain,
        region=region,
        image_id=cfg.get("image_id", "mod-burst-image"),
        max_lifetime_min=int(cfg.get("max_lifetime_min", DEFAULT_MAX_LIFETIME_MIN)),
        redact_values=(fingerprint, key_content),
    )


register_provider("oci", _oci_factory)
