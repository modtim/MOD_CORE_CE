"""GH.7b — Azure ARM burst provider (second real cloud provider for
burst-into-customer-cloud, launch-completion-megaplan group H).

Concrete provider behind the ``"azure"`` registry name. Mirrors the EC2
adapter (`services/burst_provider_ec2.py`, the template) on top of
`LibcloudBurstProvider` with the three things Azure actually needs:

  1. a GPU size map + `pick_instance_type` — map a MOD VRAM need (in GB) to
     the cheapest Azure VM size that fits (optionally restricted to an
     admin allow-list from the account's `limits.allowed_instance_types`);
  2. self-destruct user-data — Azure has NO terminate-on-shutdown flag, so
     the only on-instance safety net is the `shutdown -h +<max_lifetime_min>`
     line spliced into the custom data (via `ex_custom_data`). Note that
     `shutdown` on Azure only DEALLOCATES the VM (billing for the VM stops,
     the VM resource and its disks remain). Actual deletion happens through
     `teardown()` and the reconciler's MOD-tag sweep
     (`services/burst_reconciler.py`, `reconcile_account`) — both call
     `driver.destroy_node` on mod-burst=mod-core instances only.
  3. `probe()` — read-only credential check: list MOD-tagged instances +
     confirm the region's size catalogue is reachable, with the credential
     REDACTED from any error string.

Credential keys (parsed secret dict, see burst_accounts._creds_from_secret;
the EC2-style `provider_key`/`secret_key` spellings are accepted too):

  * ``provider_key`` (or ``key`` / ``client_id``) — Azure AD client
    (application) id  -> libcloud `key`
  * ``secret_key``   (or ``secret`` / ``client_secret``) — client secret
    -> libcloud `secret`
  * ``tenant_id``     — Azure AD tenant (directory) id
  * ``subscription_id`` — Azure subscription id
  * ``region``        — ARM region (default ``eastus``)
  * ``resource_group`` — REQUIRED. Azure needs a resource group to create a
    VM in; taken from the account limits/config (a top-level ``resource_group``
    key or ``limits.resource_group``). A clear ValueError is raised when it
    is missing — a VM created in the wrong/implicit group is an orphan MOD
    cannot reliably find and delete.

Tags are stamped through the driver's ARM tags parameter,
`create_node(ex_tags=...)`. Source: pinned `apache-libcloud>=3.8`
(requirements.txt), `libcloud/compute/drivers/azure_arm.py`,
`AzureArmNodeDriver.create_node(name, size, image, ex_resource_group=None,
ex_tags=None, ex_custom_data=None, ...)`: `ex_tags` is a dict of
str -> str applied to the VM, `ex_custom_data` is the custom data /
cloud-init string. `Provider.AZURE_ARM` ("azure_arm") selects the driver.

The libcloud import is LAZY (inside `_azure_driver_class`), exactly like
the EC2 adapter, so the API image boots fine without libcloud installed
and this module can be imported at startup (burst_provider imports it to
register the provider) without pulling libcloud into sys.modules.
"""
from __future__ import annotations

from typing import Any, Callable

from services.burst_provider import (
    MOD_TAG_KEY,
    LibcloudBurstProvider,
    mod_tag_value,
    register_provider,
)
# Same self-destruct line as EC2 — reuse the canonical implementation so the
# two adapters can never drift apart.
from services.burst_provider_ec2 import arm_self_destruct

# A burst instance that never enrolls still has a hard ceiling: default 12 h.
# The launcher can override this per account from `limits.max_lifetime_min`.
DEFAULT_MAX_LIFETIME_MIN = 720
DEFAULT_REGION = "eastus"

# ============================================================================
# GPU size map — MOD VRAM need -> Azure ARM VM size
# ============================================================================
# `vram_gb` is the per-GPU memory; `count` the number of GPUs, so a size's
# total VRAM is count * vram_gb. `price` is an approximate pay-as-you-go
# list price in $/hour (eastus), used ONLY to order "cheapest fitting" —
# the authoritative price is always re-queried from the driver's size
# catalogue at quote time, so these are for ranking, not billing.
# (approx, list price, eastus)
GPU_SIZE_MAP: dict[str, dict[str, Any]] = {
    "Standard_NC4as_T4_v3":
        {"gpu": "T4",   "count": 1, "vram_gb": 16, "vcpu": 4,  "ram_gb": 28,  "price": 0.526},
    "Standard_NV36ads_A10_v5":
        {"gpu": "A10",  "count": 1, "vram_gb": 24, "vcpu": 36, "ram_gb": 440, "price": 3.20},
    "Standard_NC24ads_A100_v4":
        {"gpu": "A100", "count": 1, "vram_gb": 80, "vcpu": 24, "ram_gb": 220, "price": 3.67},
    "Standard_NC96ads_A100_v4":
        {"gpu": "A100", "count": 4, "vram_gb": 80, "vcpu": 96, "ram_gb": 880, "price": 14.69},
    "Standard_NC40ads_H100_v5":
        {"gpu": "H100", "count": 1, "vram_gb": 94, "vcpu": 40, "ram_gb": 320, "price": 6.98},
    "Standard_NC80adis_H100_v5":
        {"gpu": "H100", "count": 2, "vram_gb": 94, "vcpu": 80, "ram_gb": 640, "price": 13.96},
}


def total_vram_gb(instance_type: str) -> int:
    """Total VRAM (count * per-GPU) for a mapped Azure size; 0 if unknown."""
    spec = GPU_SIZE_MAP.get(instance_type)
    return spec["count"] * spec["vram_gb"] if spec else 0


def pick_instance_type(
    min_vram_gb: int | float,
    allowed_instance_types: list[str] | None = None,
) -> str:
    """Pick the CHEAPEST mapped Azure VM size whose total VRAM is at least
    `min_vram_gb`.

    `allowed_instance_types` (the account's `limits.allowed_instance_types`)
    restricts the choice when provided. Raises `ValueError` with a clear
    message when the need is non-positive or no mapped size fits.
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
            f"no Azure ARM GPU VM size in the size map provides >= "
            f"{min_vram_gb} GB VRAM{where}; mapped sizes: {sorted(GPU_SIZE_MAP)}"
        )
    # Cheapest first; break ties by name for determinism.
    candidates.sort(key=lambda p: (p[0], p[1]))
    return candidates[0][1]


# ============================================================================
# AzureBurstProvider
# ============================================================================

class AzureBurstProvider(LibcloudBurstProvider):
    """BurstProvider for Microsoft Azure (via a libcloud Azure ARM
    NodeDriver).

    The driver is region-scoped (built by the factory with region, tenant,
    subscription and resource group) and is injected, so this class never
    imports libcloud itself.
    """

    def __init__(
        self,
        driver: Any,
        *,
        region: str = DEFAULT_REGION,
        resource_group: str,
        image_id: str = "mod-burst-image",
        max_lifetime_min: int = DEFAULT_MAX_LIFETIME_MIN,
        redact_values: tuple[str, ...] | list[str] = (),
    ):
        if not resource_group:
            raise ValueError(
                "azure burst requires a resource_group: Azure creates VMs "
                "inside a resource group and MOD refuses to launch into an "
                "unknown/implicit one — set it on the account (top-level "
                "resource_group or limits.resource_group)"
            )
        super().__init__(driver, instance_size_lookup=None, image_id=image_id)
        self.region = region
        self.resource_group = resource_group
        self.max_lifetime_min = int(max_lifetime_min)
        # Values to strip from any error string before it is persisted/logged.
        self._redact_values = [v for v in redact_values if v]

    # -- ARM create: tags + custom data + resource group -------------------

    def _create_node(
        self, name: str, size: str, user_data: str, tags: dict[str, Any]
    ) -> Any:
        """Azure-specific create. Source: libcloud 3.8 `azure_arm`
        `create_node(..., ex_tags=..., ex_custom_data=...,
        ex_resource_group=...)` (see module docstring).

        Tags go through `ex_tags` (a dict, applied to the VM). The
        self-destruct user-data goes through `ex_custom_data`. There is no
        terminate-on-shutdown equivalent on Azure — a `shutdown -h` in the
        custom data only deallocates the VM; deletion is left to `teardown`
        and the reconciler's MOD-tag sweep."""
        return self.driver.create_node(
            name=name,
            size=size,
            image=self.image_id,
            ex_custom_data=user_data,
            ex_tags=dict(tags),
            ex_resource_group=self.resource_group,
        )

    def provision(
        self, n: int, instance_type: str, user_data: str, tags: dict[str, Any]
    ) -> list[dict[str, Any]]:
        # Arm the hard max-lifetime self-destruct, then let the base handle
        # the MOD-tag guard, size lookup, and per-node create (which is our
        # ARM override via dynamic dispatch).
        armed = arm_self_destruct(user_data, self.max_lifetime_min)
        return super().provision(n, instance_type, armed, tags)

    # -- richer read-only credential probe -------------------------------

    def _redact(self, message: Any) -> str:
        s = str(message)
        for v in self._redact_values:
            if v:
                s = s.replace(v, "[REDACTED]")
        return s

    def probe(self) -> dict[str, Any]:
        """Read-only credential/capability probe.

        Makes only read calls — `list_nodes` (filtered to the MOD tag via
        `list_tagged`) + `list_sizes` for the region — and returns
        ``{ok, region, resource_group, visible_tagged, error}``. On ANY
        exception `ok` is False and the error string has the credential
        redacted out."""
        result: dict[str, Any] = {
            "ok": False,
            "region": self.region,
            "resource_group": self.resource_group,
            "visible_tagged": None,
            "error": None,
        }
        try:
            tagged = self.list_tagged(
                {MOD_TAG_KEY: mod_tag_value()}
            )
            # Confirm the region's size catalogue is reachable (read-only).
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

def _azure_driver_class() -> Callable[..., Any]:
    """LAZY libcloud import — the ONLY place this module touches libcloud.
    Kept as a single function so tests can monkeypatch it without any real
    libcloud (or network) present. `Provider.AZURE_ARM` ("azure_arm") is
    libcloud's Azure Resource Manager driver."""
    import libcloud.compute.providers as lp  # noqa: PLC0415 (deliberately lazy)
    from libcloud.compute.types import Provider  # noqa: PLC0415 (lazy)

    return lp.get_provider(Provider.AZURE_ARM)


def _resource_group_from_cfg(cfg: dict[str, Any]) -> str | None:
    """Resolve the Azure resource group from the account config: a top-level
    `resource_group` key wins, otherwise `limits.resource_group`."""
    rg = cfg.get("resource_group")
    if not rg:
        limits = cfg.get("limits")
        if isinstance(limits, dict):
            rg = limits.get("resource_group")
    return rg


def _azure_factory(**cfg: Any) -> AzureBurstProvider:
    """Build an AzureBurstProvider from credential kwargs.

    `cfg` is the parsed tenant secret (see burst_accounts._creds_from_secret)
    plus optional account config. Credential keys (see module docstring):
    `provider_key`/`key`/`client_id`, `secret_key`/`secret`/`client_secret`,
    `tenant_id`, `subscription_id`. Required config: `resource_group`
    (top-level or under `limits`). Optional overrides: `region`,
    `max_lifetime_min` (from the account's `limits`), `image_id`.
    """
    driver_cls = _azure_driver_class()
    key = cfg.get("provider_key") or cfg.get("key") or cfg.get("client_id")
    secret = cfg.get("secret_key") or cfg.get("secret") or cfg.get("client_secret")
    tenant_id = cfg.get("tenant_id")
    subscription_id = cfg.get("subscription_id")
    region = cfg.get("region", DEFAULT_REGION)
    resource_group = _resource_group_from_cfg(cfg)
    if not resource_group:
        raise ValueError(
            "azure burst account is missing its resource_group: set it on "
            "the account (top-level resource_group or limits.resource_group) "
            "— Azure VMs are created inside a resource group and MOD will "
            "not launch into an unknown one"
        )
    driver = driver_cls(
        key=key,
        secret=secret,
        tenant_id=tenant_id,
        subscription_id=subscription_id,
        region=region,
    )
    return AzureBurstProvider(
        driver,
        region=region,
        resource_group=resource_group,
        image_id=cfg.get("image_id", "mod-burst-image"),
        max_lifetime_min=int(cfg.get("max_lifetime_min", DEFAULT_MAX_LIFETIME_MIN)),
        redact_values=(key, secret, tenant_id, subscription_id),
    )


register_provider("azure", _azure_factory)
