"""GH.7c — Google Cloud (GCE) burst provider (third real cloud provider for
burst-into-customer-cloud, launch-completion-megaplan group H).

Concrete provider behind the ``"gcp"`` registry name. Mirrors the EC2
(`services/burst_provider_ec2.py`) and Azure
(`services/burst_provider_azure.py`) adapters on top of
`LibcloudBurstProvider` with the things GCE actually needs:

  1. a GPU size map + `pick_instance_type` — map a MOD VRAM need (in GB) to
     the cheapest GCE machine type that fits (optionally restricted to an
     admin allow-list from the account's `limits.allowed_instance_types`).
     A2/G2/A3 machine types have their GPUs built in; only the N1+T4 entry
     needs an ATTACHED accelerator, carried in the map's `accelerator`
     field and passed to create as `ex_accelerator_type` /
     `ex_accelerator_count` (the accelerator type is referenced by its full
     project+zone URL, built from the account's `project` + `datacenter`).
  2. self-destruct user-data — GCE has NO terminate-on-shutdown flag, so the
     only on-instance safety net is the `shutdown -h +<max_lifetime_min>`
     line spliced into the startup script and passed through
     `ex_metadata={"startup-script": ...}` (GCE convention: the
     `startup-script` metadata key is run as a shell script under /root on
     first boot). `shutdown` on GCE only STOPS the VM (compute billing stops;
     the VM resource is NOT deleted). Actual deletion happens through
     `teardown()` and the reconciler's MOD-tag sweep
     (`services/burst_reconciler.py`, `reconcile_account`) — both call
     `driver.destroy_node` on mod-burst=mod-core instances only.
  3. GPU scheduling — GPU VMs require an explicit maintenance policy, so
     every create passes `ex_on_host_maintenance="TERMINATE"` (the GCE
     default, live-migrate, is not supported on GPU VMs).
  4. `probe()` — read-only credential check: list MOD-labelled instances +
     confirm the zone's size catalogue is reachable, with the credential
     REDACTED from any error string.

Credential keys (parsed secret dict, see burst_accounts._creds_from_secret):

  * ``provider_key`` (or ``key``) — the service-account email (e.g.
    ``burst@my-project.iam.gserviceaccount.com``) -> libcloud `key`
  * ``secret_key`` (or ``secret``) — the service-account JSON key CONTENTS
    or its file path -> libcloud `secret`
  * ``project`` — GCP project id. REQUIRED: GCE instance and accelerator
    references are project-scoped, and a clear ValueError is raised when it
    is missing.
  * ``datacenter`` (or ``zone``) — the GCE zone (default ``us-central1-a``);
    the GCE driver is zone-scoped.

Every credential value goes into `redact_values` (key, secret, project,
datacenter). For the service-account JSON key, the full JSON string is
redacted AND its `private_key` field is extracted and redacted separately,
so a driver error that echoes part of the PEM can never reach storage or
logs.

Create kwargs (source: pinned `apache-libcloud>=3.8` — requirements.txt —
`libcloud/compute/drivers/gce.py`; `GCENodeDriver.create_node(name, size,
image, **kwargs)` forwards to `ex_create_node(..., ex_metadata=None,
ex_labels=None, ..., ex_accelerator_type=None, ex_accelerator_count=None,
ex_on_host_maintenance=None, ...)`):

  * `ex_labels` — GCE labels (a str -> str dict). GCE labels are restricted
    to lowercase `[a-z0-9_-]` (<= 63 chars), so MOD tag keys and values are
    sanitised (`sanitize_gcp_label`) before stamping. The MOD ownership tag
    (`mod-burst` = `mod-core`) is already label-safe and maps to itself, so
    `has_mod_tag` / `list_tagged` recognise the stamped label exactly — no
    adapter-side tag mapping is needed. The libcloud GCE driver nests
    labels under `node.extra["labels"]`, so this provider carries
    label-aware `teardown` / `list_tagged` overrides.
  * `ex_metadata={"startup-script": <armed user-data>}` — the GCE
    first-boot script convention (carries the self-destruct line).
  * `ex_on_host_maintenance="TERMINATE"` — required for GPU VMs.
  * `ex_accelerator_type` (full URL) + `ex_accelerator_count` — N1+T4 only.

`Provider.GCE` ("gce") selects the driver; `GCENodeDriver(key, secret,
project=..., datacenter=...)` scopes it to the account's project + zone.

The libcloud import is LAZY (inside `_gcp_driver_class`), exactly like the
EC2/Azure adapters, so the API image boots fine without libcloud installed
and this module can be imported at startup (burst_provider imports it to
register the provider) without pulling libcloud into sys.modules.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from services.burst_provider import (
    MOD_TAG_KEY,
    LibcloudBurstProvider,
    _node_dict,  # noqa: PLC2701 — same-package reuse, keeps dict shape identical to base
    has_mod_tag,
    mod_tag_value,
    register_provider,
)
# Same self-destruct line as EC2 — reuse the canonical implementation so the
# adapters can never drift apart.
from services.burst_provider_ec2 import arm_self_destruct

# A burst instance that never enrolls still has a hard ceiling: default 12 h.
# The launcher can override this per account from `limits.max_lifetime_min`.
DEFAULT_MAX_LIFETIME_MIN = 720
DEFAULT_DATA_CENTER = "us-central1-a"

# GCE label charset/length limits (labels: lowercase [a-z0-9_-], <= 63).
_LABEL_MAX_LEN = 63


# ============================================================================
# GPU size map — MOD VRAM need -> GCE machine type
# ============================================================================
# `vram_gb` is the per-GPU memory; `count` the number of GPUs, so a type's
# total VRAM is count * vram_gb. `price` is an approximate us-central1
# on-demand $/hour figure used ONLY to order "cheapest fitting" — the
# authoritative price is always re-queried from the driver's size catalogue
# at quote time, so these are for ranking, not billing.
# `accelerator` (present only on the N1+T4 entry) is the bare GCE
# accelerator type name for an ATTACHED GPU; built-in-GPU types (A2/G2/A3)
# carry no accelerator.
# (approx, list price, us-central1)
GPU_SIZE_MAP: dict[str, dict[str, Any]] = {
    "n1-standard-4":
        {"gpu": "T4",   "count": 1, "vram_gb": 16, "vcpu": 4,  "ram_gb": 15,  "price": 0.54,  "accelerator": "nvidia-tesla-t4"},
    "g2-standard-4":
        {"gpu": "L4",   "count": 1, "vram_gb": 24, "vcpu": 4,  "ram_gb": 16,  "price": 0.71},
    "a2-highgpu-1g":
        {"gpu": "A100", "count": 1, "vram_gb": 40, "vcpu": 12, "ram_gb": 85,  "price": 3.67},
    "a2-ultragpu-1g":
        {"gpu": "A100", "count": 1, "vram_gb": 80, "vcpu": 12, "ram_gb": 85,  "price": 5.07},
    "a3-highgpu-8g":
        {"gpu": "H100", "count": 8, "vram_gb": 80, "vcpu": 48, "ram_gb": 872, "price": 88.49},
}


def total_vram_gb(instance_type: str) -> int:
    """Total VRAM (count * per-GPU) for a mapped GCE type; 0 if unknown."""
    spec = GPU_SIZE_MAP.get(instance_type)
    return spec["count"] * spec["vram_gb"] if spec else 0


def pick_instance_type(
    min_vram_gb: int | float,
    allowed_instance_types: list[str] | None = None,
) -> str:
    """Pick the CHEAPEST mapped GCE machine type whose total VRAM is at
    least `min_vram_gb`.

    `allowed_instance_types` (the account's `limits.allowed_instance_types`)
    restricts the choice when provided. Raises `ValueError` with a clear
    message when the need is non-positive or no mapped type fits.
    """
    need = min_vram_gb
    try:
        need_val = float(need)
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
            f"no GCE GPU machine type in the size map provides >= "
            f"{min_vram_gb} GB VRAM{where}; mapped types: {sorted(GPU_SIZE_MAP)}"
        )
    # Cheapest first; break ties by name for determinism.
    candidates.sort(key=lambda p: (p[0], p[1]))
    return candidates[0][1]


# ============================================================================
# GCE label sanitisation
# ============================================================================

def sanitize_gcp_label(value: Any) -> str:
    """Map an arbitrary MOD tag key/value into GCE's label charset.

    GCE labels are lowercase `[a-z0-9_-]` and at most 63 chars. Non-
    conforming characters are folded to `-` (runs collapsed), leading/
    trailing `-` are stripped, and the result is truncated to 63 chars.
    The MOD ownership tag (`mod-burst` = `mod-core`) is already label-safe
    and maps to itself, so `has_mod_tag` / `list_tagged` recognise the
    stamped label exactly.
    """
    s = str(value).lower()
    s = re.sub(r"[^a-z0-9_-]", "-", s)
    s = re.sub(r"-+", "-", s)
    s = s.strip("-")[:_LABEL_MAX_LEN]
    return s or "mod"


def sanitize_gcp_labels(tags: dict[str, Any]) -> dict[str, str]:
    """Sanitise a MOD tag dict for stamping as GCE labels."""
    return {
        sanitize_gcp_label(k): sanitize_gcp_label(v) for k, v in tags.items()
    }


# ============================================================================
# GcpBurstProvider
# ============================================================================

class GcpBurstProvider(LibcloudBurstProvider):
    """BurstProvider for Google Cloud (via a libcloud GCE NodeDriver).

    The driver is project+zone-scoped (built by the factory with project
    and datacenter) and is injected, so this class never imports libcloud
    itself.
    """

    def __init__(
        self,
        driver: Any,
        *,
        datacenter: str,
        project: str,
        image_id: str = "mod-burst-image",
        max_lifetime_min: int = DEFAULT_MAX_LIFETIME_MIN,
        redact_values: tuple[str, ...] | list[str] = (),
    ):
        if not project:
            raise ValueError(
                "gcp burst requires a project: GCE instance and accelerator "
                "references are project-scoped — set `project` on the "
                "account"
            )
        if not datacenter:
            raise ValueError(
                "gcp burst requires a datacenter (GCE zone): the GCE driver "
                "is zone-scoped — set `datacenter` (or `zone`) on the "
                "account"
            )
        super().__init__(driver, instance_size_lookup=None, image_id=image_id)
        self.datacenter = datacenter
        self.project = project
        self.max_lifetime_min = int(max_lifetime_min)
        # Values to strip from any error string before it is persisted/logged.
        self._redact_values = [v for v in redact_values if v]

    # -- GCE create: labels + startup-script + maintenance + accelerator ---

    def _accelerator_type_url(self, bare_name: str) -> str:
        """Full accelerator-type URL for an attached GPU. The GCE API wants
        the URL form (a bare name is not accepted by instances.insert):
        projects/<project>/zones/<zone>/acceleratorTypes/<name>."""
        return (
            f"https://www.googleapis.com/compute/v1/projects/"
            f"{self.project}/zones/{self.datacenter}"
            f"/acceleratorTypes/{bare_name}"
        )

    def _create_node(
        self, name: str, size: str, user_data: str, tags: dict[str, Any]
    ) -> Any:
        """GCE-specific create. Source: libcloud 3.8 `gce`
        `create_node(name, size, image, **kwargs)` -> `ex_create_node(...,
        ex_metadata=..., ex_labels=..., ex_on_host_maintenance=...,
        ex_accelerator_type=..., ex_accelerator_count=...)` (see module
        docstring).

        Tags go through `ex_labels` (sanitised to GCE's label charset). The
        self-destruct startup script goes through
        `ex_metadata={"startup-script": ...}`. There is no terminate-on-
        shutdown equivalent on GCE — a `shutdown -h` in the startup script
        only STOPS the VM (compute billing stops, the VM is not deleted);
        deletion is left to `teardown` and the reconciler's MOD-tag sweep.
        GPU VMs require `ex_on_host_maintenance="TERMINATE"`; the N1+T4
        entry additionally attaches its accelerator via `ex_accelerator_
        type` / `ex_accelerator_count`."""
        kwargs: dict[str, Any] = {
            "name": name,
            "size": size,
            "image": self.image_id,
            "ex_labels": sanitize_gcp_labels(tags),
            "ex_metadata": {"startup-script": user_data},
            "ex_on_host_maintenance": "TERMINATE",
        }
        spec = GPU_SIZE_MAP.get(size)
        if spec and spec.get("accelerator"):
            kwargs["ex_accelerator_type"] = self._accelerator_type_url(
                spec["accelerator"]
            )
            kwargs["ex_accelerator_count"] = spec["count"]
        return self.driver.create_node(**kwargs)

    def provision(
        self, n: int, instance_type: str, user_data: str, tags: dict[str, Any]
    ) -> list[dict[str, Any]]:
        # Arm the hard max-lifetime self-destruct, then let the base handle
        # the MOD-tag guard, size lookup, and per-node create (which is our
        # GCE override via dynamic dispatch).
        armed = arm_self_destruct(user_data, self.max_lifetime_min)
        return super().provision(n, instance_type, armed, tags)

    # -- label-aware reads/teardown ----------------------------------------
    # The libcloud GCE driver nests labels under node.extra["labels"] (see
    # gce.py `_to_node`), so the base class's flat-tag helpers do not see
    # them. The MOD tag itself needs no character mapping (it is already
    # label-safe); only the extraction point differs.

    @staticmethod
    def _gcp_tags(node: Any) -> dict[str, str]:
        """The GCE labels of a libcloud GCE node ({} if absent)."""
        extra = getattr(node, "extra", None)
        labels = extra.get("labels") if isinstance(extra, dict) else None
        return dict(labels) if isinstance(labels, dict) else {}

    def teardown(self, node_ids: list[str]) -> list[dict[str, Any]]:
        """Destroy MOD-labelled GCE nodes ONLY. A node without the MOD
        label is refused — the same guard as the base class, applied to the
        GCE label dict."""
        by_id = {str(nd.id): nd for nd in self.driver.list_nodes()}
        out = []
        for i in node_ids:
            node = by_id.get(i)
            if node is None:
                continue
            if not has_mod_tag(self._gcp_tags(node)):
                raise PermissionError(
                    f"refusing to tear down node {i}: missing MOD tag"
                )
            self.driver.destroy_node(node)
            d = _node_dict(node)
            d["tags"] = self._gcp_tags(node)
            d["state"] = "terminated"
            out.append(d)
        return out

    def list_tagged(self, tags: dict[str, Any]) -> list[dict[str, Any]]:
        """All MOD-labelled nodes in the account (reconciler's eye).

        Requested extra tags are compared against the SANITISED form —
        labels are what the create stamped, so lookups must match that
        charset. The MOD tag itself is identity under sanitisation."""
        want = {
            sanitize_gcp_label(k): sanitize_gcp_label(v)
            for k, v in (tags or {}).items()
            if k != MOD_TAG_KEY
        }
        out = []
        for node in self.driver.list_nodes():
            labels = self._gcp_tags(node)
            if labels.get(MOD_TAG_KEY) != mod_tag_value():
                continue
            if all(labels.get(k) == v for k, v in want.items()):
                d = _node_dict(node)
                d["tags"] = labels
                out.append(d)
        return out

    # -- richer read-only credential probe -------------------------------

    def _redact(self, message: Any) -> str:
        s = str(message)
        for v in self._redact_values:
            if v:
                s = s.replace(v, "[REDACTED]")
        return s

    def probe(self) -> dict[str, Any]:
        """Read-only credential/capability probe.

        Makes only read calls — `list_nodes` (filtered to the MOD label via
        `list_tagged`) + `list_sizes` for the zone — and returns
        ``{ok, datacenter, project, visible_tagged, error}``. On ANY
        exception `ok` is False and the error string has the credential
        redacted out (including the JSON key's `private_key` field)."""
        result: dict[str, Any] = {
            "ok": False,
            "datacenter": self.datacenter,
            "project": self.project,
            "visible_tagged": None,
            "error": None,
        }
        try:
            tagged = self.list_tagged(
                {MOD_TAG_KEY: mod_tag_value()}
            )
            # Confirm the zone's size catalogue is reachable (read-only).
            sizes = self.driver.list_sizes()
            result["visible_tagged"] = len(tagged)
            result["ok"] = True
            # (size catalogue reachable; count intentionally not surfaced)
            del sizes
        except Exception as e:  # noqa: BLE001 — probe must never raise
            result["error"] = self._redact(e)
        return result


# ============================================================================
# Lazy driver factory + registration
# ============================================================================

def _gcp_driver_class() -> Callable[..., Any]:
    """LAZY libcloud import — the ONLY place this module touches libcloud.
    Kept as a single function so tests can monkeypatch it without any real
    libcloud (or network) present. `Provider.GCE` ("gce") is libcloud's
    Google Compute Engine driver; its constructor is
    `GCENodeDriver(key, secret, project=None, datacenter=None, **kwargs)`."""
    import libcloud.compute.providers as lp  # noqa: PLC0415 (deliberately lazy)
    from libcloud.compute.types import Provider  # noqa: PLC0415 (lazy)

    return lp.get_provider(Provider.GCE)


def _gcp_private_key(secret: Any) -> str | None:
    """The `private_key` PEM field of a service-account JSON key, when the
    secret is the JSON CONTENTS (a file path does not parse). Returned for
    redaction — the full JSON string alone would not catch an error message
    that echoes only the PEM."""
    if not isinstance(secret, str):
        return None
    try:
        data = json.loads(secret)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        pk = data.get("private_key")
        if isinstance(pk, str) and pk:
            return pk
    return None


def _gcp_factory(**cfg: Any) -> GcpBurstProvider:
    """Build a GcpBurstProvider from credential kwargs.

    `cfg` is the parsed tenant secret (see burst_accounts._creds_from_secret)
    plus optional account config. Credential keys (see module docstring):
    `provider_key`/`key` (service-account email), `secret_key`/`secret`
    (JSON key contents or path), `project` (REQUIRED), `datacenter`/`zone`
    (default `us-central1-a`). Optional overrides: `max_lifetime_min` (from
    the account's `limits`), `image_id`."""
    driver_cls = _gcp_driver_class()
    key = cfg.get("provider_key") or cfg.get("key")
    secret = cfg.get("secret_key") or cfg.get("secret")
    project = cfg.get("project")
    datacenter = cfg.get("datacenter") or cfg.get("zone") or DEFAULT_DATA_CENTER
    if not project:
        raise ValueError(
            "gcp burst account is missing its project: set `project` on "
            "the account — GCE instance and accelerator references are "
            "project-scoped and MOD will not launch without one"
        )
    driver = driver_cls(
        key=key,
        secret=secret,
        project=project,
        datacenter=datacenter,
    )
    redact_values = [key, secret, project, datacenter]
    pk = _gcp_private_key(secret)
    if pk:
        redact_values.append(pk)
    return GcpBurstProvider(
        driver,
        datacenter=datacenter,
        project=project,
        image_id=cfg.get("image_id", "mod-burst-image"),
        max_lifetime_min=int(cfg.get("max_lifetime_min", DEFAULT_MAX_LIFETIME_MIN)),
        redact_values=tuple(redact_values),
    )


register_provider("gcp", _gcp_factory)
