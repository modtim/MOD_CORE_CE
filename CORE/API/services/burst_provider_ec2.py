"""GH.7a — EC2 burst provider (first real cloud provider for burst-into-
customer-cloud, launch-completion-megaplan group H).

This is the concrete provider behind the ``"ec2"`` registry name. It extends
the generic `LibcloudBurstProvider` with the three things EC2 actually needs:

  1. a GPU size map + `pick_instance_type` — map a MOD VRAM need (in GB) to
     the cheapest EC2 instance type that fits (optionally restricted to an
     admin allow-list from the account's `limits.allowed_instance_types`);
  2. NATIVE auto-terminate — every instance MOD launches is created with
     `ex_terminate_on_shutdown=True` (libcloud EC2 sets
     `InstanceInitiatedShutdownBehavior=terminate`), AND a safety
     `shutdown -h +<max_lifetime_min>` line is spliced into the user-data so
     an orphan that never enrolls (or whose manager is unreachable) still
     powers itself off;
  3. `probe()` — a richer read-only credential check: list the MOD-tagged
     instances + confirm the region's size catalogue is reachable, returning
     a structured dict with the credential REDACTED from any error string.

The libcloud import is LAZY (inside `_ec2_driver_class`), exactly like the
generic factory, so the API image boots fine without libcloud installed and
this module can be imported at startup (burst_provider imports it to
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

# A burst instance that never enrolls still has a hard ceiling: default 12 h.
# The launcher can override this per account from `limits.max_lifetime_min`.
DEFAULT_MAX_LIFETIME_MIN = 720
DEFAULT_REGION = "us-east-1"

# ============================================================================
# GPU size map — MOD VRAM need -> EC2 instance type
# ============================================================================
# `vram_gb` is the per-GPU memory; `count` the number of GPUs, so a type's
# total VRAM is count * vram_gb. `price` is an approximate on-demand
# us-east-1 $/hour figure used ONLY to order "cheapest fitting" — the
# authoritative price is always re-queried from the driver's size catalogue
# at quote time, so these are for ranking, not billing.
GPU_SIZE_MAP: dict[str, dict[str, Any]] = {
    "g4dn.xlarge":  {"gpu": "T4",   "count": 1, "vram_gb": 16, "vcpu": 4,  "ram_gb": 16,   "price": 0.526},
    "g5.xlarge":    {"gpu": "A10G", "count": 1, "vram_gb": 24, "vcpu": 4,  "ram_gb": 16,   "price": 1.006},
    "g5.2xlarge":   {"gpu": "A10G", "count": 1, "vram_gb": 24, "vcpu": 8,  "ram_gb": 32,   "price": 1.212},
    "g6.xlarge":    {"gpu": "L4",   "count": 1, "vram_gb": 24, "vcpu": 4,  "ram_gb": 16,   "price": 0.70},
    "g6e.xlarge":   {"gpu": "L40S", "count": 1, "vram_gb": 48, "vcpu": 4,  "ram_gb": 16,   "price": 1.212},
    "p4d.24xlarge": {"gpu": "A100", "count": 8, "vram_gb": 40, "vcpu": 96, "ram_gb": 1152, "price": 32.772},
}


def total_vram_gb(instance_type: str) -> int:
    """Total VRAM (count * per-GPU) for a mapped EC2 type; 0 if unknown."""
    spec = GPU_SIZE_MAP.get(instance_type)
    return spec["count"] * spec["vram_gb"] if spec else 0


def pick_instance_type(
    min_vram_gb: int | float,
    allowed_instance_types: list[str] | None = None,
) -> str:
    """Pick the CHEAPEST mapped EC2 instance type whose total VRAM is at
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
            f"no EC2 GPU instance in the size map provides >= {min_vram_gb} "
            f"GB VRAM{where}; mapped types: {sorted(GPU_SIZE_MAP)}"
        )
    # Cheapest first; break ties by name for determinism.
    candidates.sort(key=lambda p: (p[0], p[1]))
    return candidates[0][1]


# ============================================================================
# Self-destruct user-data arming
# ============================================================================

def arm_self_destruct(user_data: str | None, max_lifetime_min: int) -> str:
    """Prepend a hard `shutdown -h +<max_lifetime_min>` line to `user_data`
    so an orphaned burst instance powers itself off after the ceiling even
    if it never enrolls or the manager is unreachable.

    The line is placed immediately AFTER the shebang (so the script stays a
    valid bash script and the directive runs before `set -euo pipefail`
    arms), or at the very top when there is no shebang.
    """
    directive = f"shutdown -h +{int(max_lifetime_min)}"
    ud = "" if user_data is None else str(user_data)
    if not ud.strip():
        return directive + "\n"
    lines = ud.split("\n")
    if lines and lines[0].startswith("#!"):
        return "\n".join([lines[0], directive] + lines[1:])
    return directive + "\n" + ud


# ============================================================================
# Ec2BurstProvider
# ============================================================================

class Ec2BurstProvider(LibcloudBurstProvider):
    """BurstProvider for AWS EC2 (via a libcloud EC2 NodeDriver).

    The driver is region-scoped (built by the factory with a region) and is
    injected, so this class never imports libcloud itself.
    """

    def __init__(
        self,
        driver: Any,
        *,
        region: str = DEFAULT_REGION,
        image_id: str = "mod-burst-image",
        max_lifetime_min: int = DEFAULT_MAX_LIFETIME_MIN,
        redact_values: tuple[str, ...] | list[str] = (),
    ):
        super().__init__(driver, instance_size_lookup=None, image_id=image_id)
        self.region = region
        self.max_lifetime_min = int(max_lifetime_min)
        # Values to strip from any error string before it is persisted/logged.
        self._redact_values = [v for v in redact_values if v]

    # -- native auto-terminate + MOD tag ---------------------------------

    def _create_node(
        self, name: str, size: str, user_data: str, tags: dict[str, Any]
    ) -> Any:
        """EC2-specific create: stamp the MOD tag via `ex_metadata` and set
        `ex_terminate_on_shutdown=True` (libcloud EC2 →
        InstanceInitiatedShutdownBehavior=terminate). We do NOT silently fall
        back: an instance launched WITHOUT auto-terminate would be a
        non-self-destructing orphan, so a driver that rejects the kwarg is a
        loud error, not a quiet misconfiguration."""
        return self.driver.create_node(
            name=name,
            size=size,
            image=self.image_id,
            ex_userdata=user_data,
            ex_metadata=dict(tags),
            ex_terminate_on_shutdown=True,
        )

    def provision(
        self, n: int, instance_type: str, user_data: str, tags: dict[str, Any]
    ) -> list[dict[str, Any]]:
        # Arm the hard max-lifetime self-destruct, then let the base handle
        # the MOD-tag guard, size lookup, and per-node create (which is our
        # ex_terminate_on_shutdown override via dynamic dispatch).
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
        ``{ok, region, visible_tagged, error}``. On ANY exception `ok` is
        False and the error string has the credential redacted out."""
        result: dict[str, Any] = {
            "ok": False,
            "region": self.region,
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

def _ec2_driver_class() -> Callable[..., Any]:
    """LAZY libcloud import — the ONLY place this module touches libcloud.
    Kept as a single function so tests can monkeypatch it without any real
    libcloud (or network) present."""
    import libcloud.compute.providers as lp  # noqa: PLC0415 (deliberately lazy)

    return lp.get_provider("ec2")


def _ec2_factory(**cfg: Any) -> Ec2BurstProvider:
    """Build an Ec2BurstProvider from credential kwargs.

    `cfg` is the parsed tenant secret (see burst_accounts._creds_from_secret):
    typically ``{"provider_key": ..., "secret_key": ...}``. Optional overrides:
    `region`, `max_lifetime_min` (from the account's `limits`), `image_id`.
    """
    driver_cls = _ec2_driver_class()
    key = cfg.get("provider_key") or cfg.get("key")
    secret = cfg.get("secret_key") or cfg.get("secret")
    region = cfg.get("region", DEFAULT_REGION)
    driver = driver_cls(key=key, secret=secret, region=region)
    return Ec2BurstProvider(
        driver,
        region=region,
        image_id=cfg.get("image_id", "mod-burst-image"),
        max_lifetime_min=int(cfg.get("max_lifetime_min", DEFAULT_MAX_LIFETIME_MIN)),
        redact_values=(key, secret),
    )


register_provider("ec2", _ec2_factory)
