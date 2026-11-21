"""IMPL-CE-NOPAID-6-5-P1 — per-feature capability gate for paid add-on modules.

Each add-on module maps to its OWN capability (``_MODULE_TO_CAP``; no blanket
cap). ``load_addon`` returns the module only when the deployment's entitlement
set grants that capability, and otherwise raises HTTP 402 whose message points
to /platform/features.

Rules (owner, owner 11-15 6:12 AM CT):
  * CE with no add-on present: return None, import nothing, log nothing.
  * Refuse ONLY when the capability is missing (``not es.has(cap)``). The
    ``valid`` flag of the entitlement set is NOT consulted here.
  * A refusal writes nothing: no audit row, no DB write.
  * Presence is checked with find_spec BEFORE the import, so a refused add-on
    body never runs.

Circular-import safe: no import of main/models at module scope.
"""
from __future__ import annotations

import importlib.util
from types import ModuleType
from typing import Any, Optional

from fastapi import HTTPException

from utils.paid_import import optional_paid

# utils.entitlements is import-safe at module scope (no main/models import; many
# CORE/API modules already do `from utils import entitlements`). Cap is a plain
# str-constant class, so the registry below costs nothing.
from utils.entitlements import Cap

# IMPL-CE-NOPAID-6-5-P2 — the registry: add-on module -> its OWN capability
# (owner answer (a) 11-15 6:20 AM CT: per-feature caps, no blanket cap). Keys
# are the module names as they are imported; every add-on lives in a denied
# add-on dir (CORE/API/paid_addon/** + the repo-root paid-addon/** spelling,
# open-core/manifest.yaml rows 1224-1226), so on CE `find_spec` never finds them
# and load_addon returns None without importing anything. Values are EXISTING
# Cap.* names picked from each module's own feature (no new Cap values).
_MODULE_TO_CAP: dict[str, str] = {
    # The 8 Paid compliance ORM classes (CE-NOPAID-4.9c: AccessReviewCampaign/
    # Item, LegalHold, DsarRequest, LockboxRequest, ComplianceAttestation,
    # ComplianceSubmissionHistory, ComplianceAuditorGrant) = the Fortress 3.3
    # compliance-policy family.
    "paid_addon.models_paid": Cap.COMPLIANCE_POLICY_UPLOAD,
    # FX.110 outside-collaborator access-review campaign helpers = the same
    # compliance/access-review family (they read ONLY the moved
    # AccessReviewCampaign/AccessReviewItem tables).
    "paid_addon.outside_collab": Cap.COMPLIANCE_POLICY_UPLOAD,
    # The paid tier/kind tables + closure resolver (CE-NOPAID-4.7 leg A/B): the
    # paid entitlement catalogue itself, i.e. the license/issuer add-on. The
    # package spelling is the one that exists after IMPL-CE-NOPAID-5.2-P1-D3
    # moved the repo-root `paid-addon/` layout into CORE/API/paid_addon/
    # (368ce1968e) — `paid_addon.services.paid_entitlements` is what find_spec
    # sees in a paid image built from the CE core + this add-on layer. The
    # frozen `services.paid_entitlements` NAME (utils/paid_import.py
    # PAID_ADDON_SENTINEL_MODULE, services/entitlement_provider.py:51 and the
    # 5 call sites) is how that module is LOADED (optional_paid of a repo-root
    # package dir is not wired); the gate's cap lookup goes through this key,
    # so any loader that does use it must normalize to this spelling.
    "paid_addon.services.paid_entitlements": Cap.LICENSE_SERVER,
}

REFUSAL_TEMPLATE = (
    "Not available on this plan: {module} needs the {cap} capability. "
    "See /platform/features."
)


def cap_for_module(module_name: str) -> str:
    """Return the capability registered for ``module_name``.

    An unregistered module raises ValueError, so a typo or an unlisted add-on
    cannot slip past the gate.
    """
    try:
        return _MODULE_TO_CAP[module_name]
    except KeyError:
        raise ValueError(
            f"add-on module {module_name!r} has no capability in _MODULE_TO_CAP"
        ) from None


def _addon_present(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        # A poisoned sys.modules entry (None) counts as absent (CE tripwires).
        return False


def _resolve_entitlements(db: Any) -> Any:
    from utils.entitlements import get_entitlements  # lazy: keeps import cheap

    return get_entitlements(db)


def load_addon(module_name: str, *, db: Any = None,
               entitlements: Any = None) -> Optional[ModuleType]:
    """Return the add-on module when present AND its capability is granted.

    Returns None when the add-on is absent (CE). Raises HTTPException(402) when
    the add-on is present but its capability is missing. Raises ValueError for a
    present add-on that has no registered capability.
    """
    if not _addon_present(module_name):
        return None
    cap = cap_for_module(module_name)
    es = entitlements if entitlements is not None else _resolve_entitlements(db)
    if not es.has(cap):
        raise HTTPException(
            status_code=402,
            detail=REFUSAL_TEMPLATE.format(module=module_name, cap=cap),
        )
    return optional_paid(module_name)
