"""MOD Core (open-source) — no-op credit-manager stub.

Core has no credit system. Replaces the Cloud `utils/credit_manager.py` in the
public build. `CreditManager`'s public methods are no-ops returning benign
values; any other method resolves to a no-op, so kept engine code that calls
into it (e.g. refund-on-failure paths) keeps working without a credit ledger.
"""
from __future__ import annotations

_UNLIMITED = 10 ** 9

# Mirror the Cloud module-level constant so callers that do
# `from utils.credit_manager import UNLIMITED_SENTINEL` (e.g. database.py's
# ensure_platform_unlimited_plan) get the real 1-quadrillion sentinel value
# rather than the module __getattr__ no-op below. Must stay an int.
UNLIMITED_SENTINEL = 1_000_000_000_000_000


class InsufficientCreditsError(Exception):
    """Never raised in Core; defined for import compatibility."""


class _CreditManagerMeta(type):
    """Class-level fallback: any undefined CreditManager.<name> resolves to a
    no-op, so class-method calls in kept engine code (e.g. tenant_cud's
    CreditManager.initialize_credits_for_new_subscription /
    grant_signup_welcome_bonus) keep working without a credit ledger."""

    def __getattr__(cls, name):
        def _noop(*args, **kwargs):
            return None
        return _noop


class CreditManager(metaclass=_CreditManagerMeta):
    """All operations are free no-ops in Core."""

    @staticmethod
    def get_credit_balance(db=None, tenant_id: int = 1) -> dict:
        return {"balance": _UNLIMITED, "unlimited": True}

    @staticmethod
    def deduct_credits(*args, **kwargs) -> bool:
        return True

    @staticmethod
    def add_credits(*args, **kwargs) -> bool:
        return True

    @staticmethod
    def log_credit_usage(*args, **kwargs):
        return None

    @staticmethod
    def refund_credits_for_failed_workflow(*args, **kwargs):
        return None

    @staticmethod
    def refund_credits_for_port_failure(*args, **kwargs):
        return None

    @staticmethod
    def calculate_workflow_credits(*args, **kwargs) -> dict:
        return {"total_credits": 0, "node_costs": {}}

    def __getattr__(self, name):  # instance-level fallback
        def _noop(*args, **kwargs):
            return None
        return _noop


def pricing_model(db=None, *args, **kwargs) -> str:
    """Core has no pricing models; never selects the task_profile branch."""
    return "legacy_gb_multiplier"


def estimate_task_profile(db=None, *args, **kwargs):
    """Core is free: (0 credits, empty quote) with the Cloud return shape.

    routers/workflows_domains/workflows.py:839-858 unpacks this tuple and
    reads quote["reason"] + quote["nodes"]; the guard at :829 keeps CE off
    this branch, but the shape stays correct if it is ever reached.
    """
    return 0, {"credits": 0, "reason": "MOD Core: free", "nodes": [],
               "runs_per_node": 1, "data_bytes": 0}


def __getattr__(name: str):
    """Module-level fallback for any other name imported from here."""
    def _noop(*args, **kwargs):
        return None
    return _noop
