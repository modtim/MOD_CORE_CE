"""services/productivity_floor_rollout.py — sz-8.1..8.7 (productivity-floor-primitives
tasks.md 8.1-8.7): the per-phase / per-tenant rollout gate service.

The productivity-floor features (cost economics, live debugging, CRDT collab,
triggers/outputs, AI primitives, domain primitives, the DSL devkit) ship to
tenants gradually, phase by phase, so the fleet can measure a feature on a
slice of tenants before it is turned on for everyone. This module is the
GATE ONLY: nothing calls it yet (wiring each feature to it is a later task),
so registering these keys changes no live behaviour.

Settings (registered idempotently at import, group="productivity_floor",
resolution = tenant row > platform row > registered default like every other
tunable — see [[settings-not-env-files]]): for every feature key F in
`FEATURE_KEYS` (the 7 top-level features plus the `oauth.<provider>` and
`ai_category.<category>` sub-keys):

- `rollout.<F>.percent`       int 0..100, default 100
- `rollout.<F>.allow_tenants` list, default []
- `rollout.<F>.deny_tenants`  list, default []

Default 100 is deliberate: the gate must not hide a feature that is already
live, so "no rows written yet" means "everyone has it". Turning a feature off
for some tenants is an explicit write (percent < 100, or a deny row).

The bucket is deterministic — `int(sha256("<feature>:<tenant_id>")[:8], 16)
% 100` — so the same tenant is in the same slice across every call, restart
and process, and raising `percent` can only ever ADD tenants (the monotonic
property the tests lock). No RNG, no per-call salt: a rollout that flips
between requests is the classic tenant-flicker bug.

A settings-store failure NEVER hides a live feature: the read falls back to
the registered defaults (percent 100, empty lists) and logs the exception
class name once per feature. That is fail-OPEN on purpose — the opposite of
the auto-build flag, which fails closed because "on" there starts an
autonomous build. Here "on" is the current shipped behaviour, so the safe
answer when the settings layer is unhappy is "the feature is live".
"""
from __future__ import annotations

import hashlib
import logging
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from services import platform_settings as psr

logger = logging.getLogger(__name__)

#: The platform tenant. `dependencies.PLATFORM_TENANT_ID` is only function-local
#: there, so services that need it declare their own copy (same convention as
#: services/asset_inventory.py). Dogfood first (spec 8.1): the platform tenant
#: always has every productivity-floor feature.
PLATFORM_TENANT_ID = 1

GROUP = "productivity_floor"

#: The seven productivity-floor phases, in rollout order (sz-8.1 .. sz-8.7).
FEATURES: Tuple[str, ...] = (
    "cost_economics",
    "live_debugging",
    "crdt_collab",
    "triggers_outputs",
    "ai_primitives",
    "domain_primitives",
    "dsl_devkit",
)

# --- Sub-key vocabularies --------------------------------------------------
# Read from the owning registries (never re-hardcoded) so a new provider or
# category automatically gets its own rollout keys. The fallbacks exist for a
# build where the owning module is unavailable (CE strips, import order); the
# gate still works, it just cannot see the extra granularity there.
_OAUTH_PROVIDER_FALLBACK = ("airtable", "github", "google", "linear",
                            "microsoft365", "notion", "slack")
_AI_CATEGORY_FALLBACK = ("embeddings", "eval", "fine_tune", "llm_call",
                         "rag_pattern", "tool_use", "vector_search")


def _oauth_providers() -> Tuple[str, ...]:
    try:
        from services import oauth_broker
        return tuple(sorted(oauth_broker.PROVIDERS))
    except Exception as exc:  # noqa: BLE001 — degrade to the reviewed fallback
        logger.warning("productivity_floor_rollout: oauth provider registry "
                       "unavailable (%s); using the reviewed fallback list",
                       type(exc).__name__)
        return _OAUTH_PROVIDER_FALLBACK


def _ai_categories() -> Tuple[str, ...]:
    try:
        from services.ai_primitives.contracts import CATEGORIES
        return tuple(CATEGORIES)
    except Exception as exc:  # noqa: BLE001 — degrade to the reviewed fallback
        logger.warning("productivity_floor_rollout: ai-primitive category "
                       "registry unavailable (%s); using the reviewed fallback "
                       "list", type(exc).__name__)
        return _AI_CATEGORY_FALLBACK


#: A sub-key is only live where its parent feature is live: an OAuth connection
#: rides the triggers/outputs surface, an AI category rides ai_primitives.
PARENT_BY_PREFIX: Dict[str, str] = {
    "oauth.": "triggers_outputs",
    "ai_category.": "ai_primitives",
}

SUBKEYS: Tuple[str, ...] = (
    tuple(f"oauth.{p}" for p in _oauth_providers())
    + tuple(f"ai_category.{c}" for c in _ai_categories())
)

#: Everything the gate knows about. Anything outside this tuple is a
#: programmer error (a typo in a call site), never a silent False.
FEATURE_KEYS: Tuple[str, ...] = FEATURES + SUBKEYS

PERCENT_SUFFIX = "percent"
ALLOW_SUFFIX = "allow_tenants"
DENY_SUFFIX = "deny_tenants"

# --- Bucket -----------------------------------------------------------------


def _bucket(feature: str, tenant_id: int) -> int:
    """Stable 0..99 bucket for (feature, tenant).

    sha256 of the literal `"<feature>:<tenant_id>"` — no RNG, no salt, no
    process state — so the slice is identical across calls, restarts and
    machines, and `bucket < percent` is monotonic in `percent` (raising the
    percentage only ever adds tenants; it can never switch an enabled tenant
    off). The feature name is in the preimage so two features at 10% pick
    different 10% slices, which is what makes per-phase measurement honest.
    """
    digest = hashlib.sha256(f"{feature}:{tenant_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


#: The registered defaults, spelled out here so the fallback path and the
#: registry can never drift apart.
DEFAULTS: Dict[str, object] = {
    PERCENT_SUFFIX: 100,
    ALLOW_SUFFIX: [],
    DENY_SUFFIX: [],
}


def _percent_key(feature: str) -> str:
    return f"rollout.{feature}.{PERCENT_SUFFIX}"


def _allow_key(feature: str) -> str:
    return f"rollout.{feature}.{ALLOW_SUFFIX}"


def _deny_key(feature: str) -> str:
    return f"rollout.{feature}.{DENY_SUFFIX}"


def _read(key: str, db: Optional[Session], tenant_id: Optional[int],
          feature: str, suffix: str):
    """One settings read, falling back to the registered default.

    Returns (value, error_class_name_or_None). A non-None name means the
    settings layer raised: the caller answers with the registered defaults
    (percent 100, empty lists = the current shipped behaviour) and logs the
    exception class name ONCE for the feature, not once per key. A broken
    settings store must never hide a feature that is already live.
    """
    try:
        return psr.get_setting(key, db=db, tenant_id=tenant_id), None
    except Exception as exc:  # noqa: BLE001 — fail open (feature stays live)
        return DEFAULTS[suffix], type(exc).__name__


# --- Gate -------------------------------------------------------------------


def is_enabled(db: Optional[Session], tenant_id: int, feature: str) -> bool:
    """True when `feature` is rolled out to `tenant_id`.

    Order (spec 8.1-8.7): unknown feature -> ValueError (a typo must not
    silently disable a feature); platform tenant -> always True (dogfood
    first, and the platform tenant cannot be switched off per-tenant even if
    someone lists it in a deny row); deny list -> False; allow list -> True;
    otherwise the deterministic bucket against `percent`.

    A sub-key (`oauth.<provider>`, `ai_category.<category>`) additionally
    requires its parent feature to be enabled: an OAuth connection is only
    useful where triggers/outputs are, an AI category only where the AI
    primitives are.

    `db=None` lets the settings registry open its own short session.
    """
    if feature not in FEATURE_KEYS:
        raise ValueError(
            f"unknown rollout feature {feature!r}; known: "
            f"{', '.join(FEATURE_KEYS)}"
        )
    tenant_id = int(tenant_id)
    if tenant_id == PLATFORM_TENANT_ID:
        return True

    parent = _parent_of(feature)
    if parent is not None and not is_enabled(db, tenant_id, parent):
        return False

    percent, err_p = _read(_percent_key(feature), db, tenant_id, feature,
                           PERCENT_SUFFIX)
    allow, err_a = _read(_allow_key(feature), db, tenant_id, feature,
                         ALLOW_SUFFIX)
    deny, err_d = _read(_deny_key(feature), db, tenant_id, feature,
                        DENY_SUFFIX)
    errors = [e for e in (err_p, err_a, err_d) if e is not None]
    if errors:
        _log_degraded(feature, errors)
        return True

    if tenant_id in {int(t) for t in deny}:
        return False
    if tenant_id in {int(t) for t in allow}:
        return True
    return _bucket(feature, tenant_id) < int(percent)


#: Features whose degraded-settings line has already been logged. "Once per
#: feature" (spec 8.7) means a broken store is loud the first time it is hit,
#: not on every request; `reset_degraded_logging()` is the test seam.
_DEGRADED_FEATURES: set = set()


def _log_degraded(feature: str, errors: List[str]) -> None:
    if feature in _DEGRADED_FEATURES:
        return
    _DEGRADED_FEATURES.add(feature)
    logger.warning("productivity_floor_rollout: settings for %r unreadable "
                   "(%s); evaluated with the registered defaults (percent 100,"
                   " empty lists) so a live feature is never hidden",
                   feature, "/".join(sorted(set(errors))))


def reset_degraded_logging() -> None:
    """Test seam: forget which features have logged a degraded read."""
    _DEGRADED_FEATURES.clear()


def rollout_status(db: Optional[Session], tenant_id: int) -> Dict[str, bool]:
    """The whole gate for one tenant: {feature_key: enabled}.

    Shape for the future rollout-status endpoint/UI (sz-8.x wiring task).
    Every key in `FEATURE_KEYS` is present, in registry order, so the UI can
    render a stable table without knowing the sub-key vocabularies.
    """
    return {key: is_enabled(db, tenant_id, key) for key in FEATURE_KEYS}


def _parent_of(feature: str) -> Optional[str]:
    for prefix, parent in PARENT_BY_PREFIX.items():
        if feature.startswith(prefix):
            return parent
    return None


# --- Registration -----------------------------------------------------------


def _validate_percent(value: int) -> None:
    """0..100 only: a percentage outside that range is a typo that would
    silently mean "everyone" (101) or "nobody" (-1)."""
    if not 0 <= int(value) <= 100:
        raise ValueError(f"rollout percentage must be 0..100, got {value!r}")


def _validate_tenant_ids(value: List) -> None:
    """Tenant ids only: a non-integer entry in an allow/deny row is a typo,
    and a silently-skipped entry would make a tenant think it was opted out
    when it was not."""
    for item in value:
        int(item)


def _register_settings() -> None:
    """Declare every rollout key once (register() is a no-op on re-import).

    Idempotent + lazy-vocabulary: the sub-keys come from `FEATURE_KEYS`, which
    was built from the oauth_broker / ai_primitives registries at import, so a
    new provider or category automatically gets its own percent/allow/deny
    triple and no key is ever hand-listed.
    """
    for feature in FEATURE_KEYS:
        parent = _parent_of(feature)
        parent_note = (f" Requires its parent ({parent}) to be enabled too."
                       if parent else "")
        psr.register(
            _percent_key(feature),
            int,
            DEFAULTS[PERCENT_SUFFIX],
            validator=_validate_percent,
            group=GROUP,
            description="sz-8.1..8.7 — share (0..100) of NON-platform tenants that "
                        f"gets the {feature} productivity-floor feature, by "
                        f"deterministic sha256('{feature}:<tenant_id>') bucket. "
                        "Default 100 = already live for everyone, so wiring the "
                        "gate never hides a shipped feature; drop it to measure "
                        f"{feature} on a slice first. Platform tenant 1 is always "
                        f"on (dogfood first).{parent_note}",
        )
        psr.register(
            _allow_key(feature),
            list,
            list(DEFAULTS[ALLOW_SUFFIX]),
            validator=_validate_tenant_ids,
            group=GROUP,
            description=f"sz-8.1..8.7 — tenants force-ENABLED for {feature} "
                        "regardless of the bucket (early-access tenants). Beats "
                        "the bucket, loses to deny_tenants.",
        )
        psr.register(
            _deny_key(feature),
            list,
            list(DEFAULTS[DENY_SUFFIX]),
            validator=_validate_tenant_ids,
            group=GROUP,
            description=f"sz-8.1..8.7 — tenants force-DISABLED for {feature} "
                        "regardless of the bucket or allow list (opt-out). "
                        "Beats allow_tenants; never applies to the platform "
                        "tenant, which cannot be switched off.",
        )


_register_settings()
