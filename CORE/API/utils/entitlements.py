"""MOD Community Edition — entitlements stub.

Community Edition has no tiers, no gates and no limits: every capability is
granted (except the ones listed in _DENIED_CAPS, which are denied with a neutral
reason) and every numeric limit is unlimited. This module replaces the full
resolver in the Community Edition source tree and keeps its public surface, so
the callers that ship with Community Edition work unchanged. There is nothing
here to configure, flip or bypass.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional

# Same value the rest of the codebase uses for "effectively unlimited".
UNLIMITED = 1_000_000_000_000_000

# The only edition name in the open core.
CANONICAL_TIERS = ("community",)


class _Names(type):
    """`Cap.<NAME>` resolves to the lowercase wire name for any attribute, so no
    capability list is defined here."""

    def __getattr__(cls, name: str) -> str:
        if name.startswith("_"):
            raise AttributeError(name)
        return name.lower()


class Cap(metaclass=_Names):
    """Capability names. Every name resolves and every one is granted."""


class Limit:
    """Numeric limit keys. Every one is unlimited."""
    SEATS = "max_seats"
    STORAGE_GB = "max_storage_gb"
    CREDITS_PER_MONTH = "credits_per_month"
    CONCURRENT = "max_concurrent_processes"
    CPU_HOURS_MONTHLY = "max_cpu_hours_monthly"
    EGRESS_GB = "included_egress_gb"
    MANAGER_NODES = "max_manager_nodes"
    WORKER_NODES = "max_worker_nodes"
    TENANTS = "max_tenants"


_LIMIT_KEYS = tuple(v for k, v in vars(Limit).items() if k.isupper())

# The capability names /platform/features advertises: exactly the features that
# really work in Community Edition. The Dashboard and Workflow Builder lock a
# control when its name is missing from this list, so a name is listed only if the
# feature behind it ships and is free. (Capabilities whose code is not in this
# edition are not listed; the controls that read them are hidden instead.)
_LISTED_NAMES = (
    "advanced_scheduling", "byo_s3", "custom_nodes", "custom_registry",
    "k8s_backend", "k8s_multi_cluster", "show_timing", "stack_edit",
    "usage_metering",
)


# Capabilities that are NOT part of Community Edition. Each one is denied with a
# neutral reason and is absent from the community set. Every other capability is
# granted: CE has no quantity caps and no licence gates beyond this list.
_DENIED_CAPS = frozenset({Cap.HA_CONTROL_PLANE})


class _AllGranted(frozenset):
    """A feature set that contains every capability name except the denied ones."""

    def __contains__(self, item) -> bool:  # noqa: D105
        return item not in _DENIED_CAPS


@dataclass(frozen=True)
class EntitlementSet:
    features: FrozenSet[str]
    limits: Dict[str, int]
    source: str
    valid: bool = True
    tier_name: str = "community"
    expires_at: Optional[str] = None

    def has(self, capability: str) -> bool:
        return capability not in _DENIED_CAPS

    def limit(self, key: str, default: int = 0) -> int:
        return UNLIMITED

    def is_unlimited(self, key: str) -> bool:
        return True

    def remaining_headroom(self, key: str, used: int) -> Optional[int]:
        return None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    granted: bool
    reason: Optional[str]
    mode: str


_COMMUNITY = EntitlementSet(
    features=_AllGranted(_LISTED_NAMES),
    limits={k: UNLIMITED for k in _LIMIT_KEYS},
    source="baseline",
    valid=True,
    tier_name="community",
)

_ALLOWED = Decision(True, True, None, "open")
_DENIED_DECISION = Decision(False, False, "not available in this edition", "enforce")


def is_saas() -> bool:
    return False


def get_entitlements(db=None, tenant=None) -> EntitlementSet:
    return _COMMUNITY


def entitlements_from_plan(plan=None, tenant=None) -> EntitlementSet:
    return _COMMUNITY


def evaluate_feature(capability: str, es: Optional[EntitlementSet] = None) -> Decision:
    if capability in _DENIED_CAPS:
        return _DENIED_DECISION
    return _ALLOWED


def evaluate_limit(limit_key: str, used: int = 0, es: Optional[EntitlementSet] = None,
                   requesting: int = 1) -> Decision:
    return _ALLOWED
