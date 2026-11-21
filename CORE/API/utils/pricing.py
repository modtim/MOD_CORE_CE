"""MOD Core (open-source) — no-op pricing stub.

Core is free; all credit prices are zero. Replaces the Cloud `utils/pricing.py`
in the public build.
"""
from __future__ import annotations


def get_postpaid_credit_price(db=None, tenant_id=None) -> int:
    """Free in Core."""
    return 0


def get_overage_credit_price(db=None, tenant_id=None) -> int:
    """Free in Core."""
    return 0


def __getattr__(name: str):
    def _noop(*args, **kwargs):
        return 0
    return _noop
