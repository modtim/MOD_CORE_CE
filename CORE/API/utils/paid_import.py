"""GT.D4b — call-time imports of Paid modules that are absent in the CE carve.

open-core/build_public_repo.py drops the Paid modules from the CE tree, so a
function-level `from services.noah import ...` raises ImportError at request
time on CE. Two shapes cover every site:

  * optional_paid("services.egress_meter") -> module | None
      For side effects (metering, fleet helpers). On CE the caller
      skips the work.
  * require_paid("services.noah", "NOAH idle-worker harvest") -> module
      For a user-facing Paid feature. On CE it raises HTTP 402 with a
      "This feature" detail, so the client shows the upgrade path, not a 500.

Only a missing module (or a missing Paid submodule) counts as "absent". An
ImportError raised *inside* a present Paid module is a real bug and is
re-raised.
"""
from __future__ import annotations

import importlib
from types import ModuleType
from typing import Optional

from fastapi import HTTPException
import importlib.util
import os
import sys


# ---------------------------------------------------------------------------
# FX.162.p1 (owner Q157: MOD CE must not do anything with compliance) — the
# ONE edition predicate. "paid tree present" == the paid sentinel MARKER
# module services/edition_sentinel.py is importable in this image (open-core/manifest.yaml
# denies CORE/API/services/edition_sentinel.py wholesale, so the CE carve strips
# it). CUT-A2 (post-ce-launch L160) decoupled the sentinel from services/wizard_ml:
# wizard_ml is a paid FEATURE package that moves through the manifest (api_allow
# rows, deny_exceptions, future re-scopes), and a feature that ever shipped in
# CE would silently flip every edition gate to paid. The marker module's only
# job is to mark the tree, so it cannot be re-scoped without an owner decision.
# FX.162.p1 (owner Q157: MOD CE must not do anything with compliance) history:
# main.py's /v1/platform/features edition line, node_scanner._is_ce_edition and
# compliance_environments all consult this helper; every edition check goes
# through here.
# ---------------------------------------------------------------------------
PAID_SENTINEL_MODULE = "services.edition_sentinel"
# IMPL-CE-NOPAID-5-2 D2 (owner-accepted 11-15): a SECOND sentinel. A paid image
# built from the CE core tag plus the paid add-on (CORE/API/paid_addon/) carries
# services.paid_entitlements even where services.wizard_ml is not present, so
# either module being findable means "paid tree". No env var, no setting.
PAID_ADDON_SENTINEL_MODULE = "services.paid_entitlements"


def _module_findable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def is_paid_tree() -> bool:
    """True iff this image carries the paid (Pro/SaaS) tree.

    Same technique as WS2.3/scheduler._module_available: the CE carve strips
    whole modules out of the tree, so the paid sentinel packages' absence is
    the CE signal. Either sentinel (PAID_SENTINEL_MODULE or
    PAID_ADDON_SENTINEL_MODULE) being findable marks a paid tree. find_spec
    only: nothing is imported. A poisoned (sys.modules[name] = None) entry —
    how the CE tripwire tests simulate the carve — counts as absent too.
    """
    return _module_findable(PAID_SENTINEL_MODULE) or _module_findable(
        PAID_ADDON_SENTINEL_MODULE
    )


def edition_test_override() -> Optional[bool]:
    """The ``MOD_CE_EDITION`` override, honoured ONLY inside a pytest process.

    Tests force the CE / paid branch with it instead of faking module absence.
    Outside pytest it is ignored, so an operator (or anyone who can set an
    environment variable) cannot flip an edition gate: ``pytest`` is never
    imported by the running API. Returns None when there is no override to apply.
    """
    if "pytest" not in sys.modules:
        return None
    raw = os.getenv("MOD_CE_EDITION")
    if raw is None:
        return None
    return raw.strip().lower() in ("1", "true", "yes", "on")


def is_ce_edition() -> bool:
    """True iff running the CE (open-source) carve.

    Inferred from the sentinel module's presence. ``MOD_CE_EDITION`` can force
    the answer, but only under pytest (see ``edition_test_override``); it is
    not a behaviour tunable.
    """
    override = edition_test_override()
    if override is not None:
        return override
    return not is_paid_tree()


def edition() -> str:
    """``"ce"`` or ``"pro"`` for this install (saas is decided by the caller)."""
    return "ce" if is_ce_edition() else "pro"


def _is_absent(exc: ImportError, module_name: str) -> bool:
    missing = getattr(exc, "name", None) or ""
    return bool(missing) and (module_name == missing or module_name.startswith(missing + "."))


def optional_paid(module_name: str) -> Optional[ModuleType]:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        if _is_absent(exc, module_name):
            return None
        raise


def require_paid(module_name: str, feature: str) -> ModuleType:
    mod = optional_paid(module_name)
    if mod is None:
        raise HTTPException(
            status_code=402,
            detail=f"Not part of this edition: {feature}.",
        )
    return mod


# First-party top-level packages that open-core/manifest.yaml carves paid
# submodules out of. An ImportError whose missing name does NOT start with one
# of these is a third-party dependency problem (a broken deploy), never an
# edition carve — must not be swallowed as "feature unavailable".
_FIRST_PARTY_PREFIXES = (
    "services.", "routers.", "crud.", "utils.", "node_builder.",
    "node_definitions_domains.", "middleware.", "scim.", "scheduler.",
)


def classify_missing_import(exc: BaseException) -> Optional[str]:
    """Return the missing dotted module name if `exc` (an ImportError /
    ModuleNotFoundError raised anywhere in the app) should be treated as
    "this edition doesn't ship that module" — else None.

    Three conditions, all required (mirrors `_is_absent` + `is_paid_tree`):
      1. This is a CE build (`is_ce_edition()`). On a paid build nothing is
         legitimately missing, so never reclassify there — an ImportError on
         paid is always a real bug.
      2. `exc.name` is set and starts with one of `_FIRST_PARTY_PREFIXES` —
         i.e. it's one of OUR modules under a dir the CE carve prunes, not an
         unrelated third-party pip package.
      3. `importlib.util.find_spec(name)` is None — genuinely absent from this
         install (not e.g. a transient/partial import), same check
         `is_paid_tree()` uses for the sentinel.
    """
    if not is_ce_edition():
        return None
    missing = getattr(exc, "name", None) or ""
    if not missing or not missing.startswith(_FIRST_PARTY_PREFIXES):
        return None
    try:
        if importlib.util.find_spec(missing) is not None:
            return None
    except (ImportError, ValueError, ModuleNotFoundError):
        pass  # find_spec itself failing to resolve it still counts as absent
    return missing
