"""CE-NOPAID-4.5a (launch-completion-megaplan) — the entitlement provider seam.

Design seam 2 (design note on the add-on boundary options):
"Entitlement provider: a small interface in `services/entitlement_guard.py`
(`EntitlementProvider.check_*`); CE ships a null provider (always allow, no
caps, per the owner rule); the non-CE editions register the real one".

This module is SHARED (core, ships in the CE carve). It owns exactly three
things and nothing else:

  * the interface — `EntitlementProvider`, five methods whose signatures are
    byte-identical to today's free functions in
    `services/entitlement_guard.py` (`:55 check_run`, `:113
    assert_cross_region_audit_allowed`, `:127 preflight_run`, `:255
    check_workflow_run`, `:320 check_stack_run`), so a call site can move to
    the provider without changing how it passes arguments;
  * the null provider — the base class IS the null provider: every method
    returns the allow value (`True` / `[]`) without touching `db`, `models`,
    or any table. No caps, no tiers, no kinds, no 402/403 (owner rule: CE has
    no quantity caps and no licence gates);
  * the resolution point — `get_entitlement_provider()` returns the null
    provider unless the non-CE provider module
    (`services.paid_entitlements`, created by CE-NOPAID-4.7) is importable in
    this image. Resolution is call-time and cached, so it never depends on a
    boot hook running first (spec risk R3: a non-CE image that registered late
    must not silently run the null provider).

`register_paid_entitlement_provider()` is the non-CE boot hook the plan names;
it is idempotent and never raises / never 402s — on CE (add-on absent) it is a
no-op and the null provider stays.

Nothing here imports a non-CE module at module scope: `optional_paid` runs inside
the function, so `import services.entitlement_provider` succeeds in a fresh
process on the CE tree (the 4.5a proof).
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from utils import paid_import as _paid_import

logger = logging.getLogger(__name__)

# The non-CE add-on module CE-NOPAID-4.7 moves the real check bodies into. Absent
# in the CE carve (open-core/manifest.yaml denies `CORE/API/paid_addon/**`, the one
# add-on home per CE-NOPAID-5.2 part 1), which is the edition signal `utils/paid_import`
# already uses.
PAID_ENTITLEMENTS_MODULE = "services.paid_entitlements"

# The five names the provider exposes; `register_paid_entitlement_provider()`
# wraps the same five names off the non-CE module.
PROVIDER_METHODS = (
    "check_run",
    "preflight_run",
    "check_workflow_run",
    "check_stack_run",
    "assert_cross_region_audit_allowed",
)


class EntitlementProvider:
    """The provider interface; the base class is also the CE null provider.

    Every method returns the allow value and ignores its arguments: no DB read,
    no table, no cap/kind/tier name appears in this class (the CE-extract gate
    grep in plan 4.8 counts 0 entitlement kind/cap names in the CE entitlement
    path). A non-CE provider subclasses this and overrides the five methods with
    the real bodies.
    """

    def check_run(
        self,
        db: Session,
        tenant_id: Optional[int],
        node_kind: Optional[str],
        trial_node_kind: Optional[str] = None,
        *,
        check_marketplace: bool = True,
        metric_site: str = "enqueue",
    ) -> bool:
        """Null: always allow the enqueue. Mirrors `entitlement_guard.py:55`."""
        return True

    def preflight_run(
        self,
        db: Session,
        tenant_id: Optional[int],
        workflow_version,
        *,
        graph: Optional[dict] = None,
        check_listing: bool = True,
        metric_site: str = "preflight",
    ) -> list:
        """Null: nothing is purchase-required. Mirrors `entitlement_guard.py:127`."""
        return []

    def check_workflow_run(
        self,
        db: Session,
        tenant_id: Optional[int],
        workflow_listing_id: Optional[int],
    ) -> bool:
        """Null: always allow. Mirrors `entitlement_guard.py:255`."""
        return True

    def check_stack_run(
        self,
        db: Session,
        tenant_id: Optional[int],
        stack_listing_ids: Optional[Iterable[int]],
    ) -> bool:
        """Null: always allow. Mirrors `entitlement_guard.py:320`."""
        return True

    def assert_cross_region_audit_allowed(self, db=None) -> bool:
        """Null: single-region export stays ungated. Mirrors `:113`."""
        return True


def _paid_provider(paid_module):
    """Wrap the five free functions of a non-CE module into a provider subclass.

    Delegation is by name lookup at call time, so the wrapper keeps working
    whatever order the non-CE boot hook and the first request happen in.
    """

    class _PaidEntitlementProvider(EntitlementProvider):
        """Real (non-CE) provider: the bodies live in `services.paid_entitlements`
        (CE-NOPAID-4.7 moves `services/entitlement_guard.py`'s five functions
        there verbatim). Behaviour must be IDENTICAL to today's: same raises,
        same fail-open paywall half / fail-closed marketplace half, same exempt
        rules, same `active_filter` semantics."""

        def _call(self, name, *args, **kwargs):
            fn = getattr(paid_module, name, None)
            if fn is None:
                raise AttributeError(
                    f"services.paid_entitlements is missing {name} — the add-on "
                    "provider cannot be registered half-built (silent null "
                    "provider = an add-on gate leak)"
                )
            return fn(*args, **kwargs)

        def check_run(self, db, tenant_id, node_kind, trial_node_kind=None, *,
                      check_marketplace=True, metric_site="enqueue"):
            return self._call(
                "check_run", db, tenant_id, node_kind, trial_node_kind,
                check_marketplace=check_marketplace, metric_site=metric_site)

        def preflight_run(self, db, tenant_id, workflow_version, *,
                          graph=None, check_listing=True, metric_site="preflight"):
            return self._call(
                "preflight_run", db, tenant_id, workflow_version,
                graph=graph, check_listing=check_listing, metric_site=metric_site)

        def check_workflow_run(self, db, tenant_id, workflow_listing_id):
            return self._call("check_workflow_run", db, tenant_id, workflow_listing_id)

        def check_stack_run(self, db, tenant_id, stack_listing_ids):
            return self._call("check_stack_run", db, tenant_id, stack_listing_ids)

        def assert_cross_region_audit_allowed(self, db=None):
            return self._call("assert_cross_region_audit_allowed", db)

    return _PaidEntitlementProvider()


# Resolution cache: None = not resolved yet. A resolved provider is never
# re-resolved, so the first call decides the edition for the process (the
# image's module set cannot change mid-process).
_RESOLVED_PROVIDER = None


def get_entitlement_provider(db=None) -> EntitlementProvider:
    """The provider for this image: the non-CE one if importable, else the null.

    `db` is accepted (and ignored) so a call site can pass its session
    positionally, matching the five free functions it replaces.
    """
    global _RESOLVED_PROVIDER
    if _RESOLVED_PROVIDER is None:
        paid_module = _paid_import.optional_paid(PAID_ENTITLEMENTS_MODULE)
        _RESOLVED_PROVIDER = (
            _paid_provider(paid_module) if paid_module is not None
            else EntitlementProvider()
        )
    return _RESOLVED_PROVIDER


def register_paid_entitlement_provider(db=None) -> None:
    """Non-CE boot hook: register the real provider. Never raises, never 402s.

    On the CE tree the add-on is absent and this is a no-op — the null provider
    remains the answer to `get_entitlement_provider()`. Idempotent, so a boot
    hook that runs more than once (or a boot hook plus a first request) is safe.
    """
    global _RESOLVED_PROVIDER
    if _RESOLVED_PROVIDER is not None:
        return
    paid_module = _paid_import.optional_paid(PAID_ENTITLEMENTS_MODULE)
    if paid_module is None:
        return
    _RESOLVED_PROVIDER = _paid_provider(paid_module)
