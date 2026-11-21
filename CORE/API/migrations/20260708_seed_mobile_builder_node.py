"""Seed / heal the "Build Mobile App" (mobile_build) platform node on boot.

Idempotent. Wraps the existing ``seed_mobile_builder`` (create-if-missing +
heal accepted_secrets / input_schema) so the node self-applies on every
deploy via start-main-server.sh Step 6c — closing the "no migration runner"
gap where a seed added after a machine's first boot never fires (the
background _startup_system_maintenance batch doesn't reliably re-run new
entries). Matched by (node_kind='mobile_build', tenant_id IS NULL).

The keystore secret seed is attempted best-effort — a missing/absent
keystore must NOT block node registration (signing degrades to debug; the
node is still usable and the app-build gate depends only on the node + the
toolchain marker).
"""
from __future__ import annotations

import argparse
import os
import sys


def _get_session():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    return _db.SessionLocal()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    session = _get_session()
    import models  # noqa: F401

    def _present() -> bool:
        return session.query(models.NodeDefinition).filter(
            models.NodeDefinition.node_kind == "mobile_build",
            models.NodeDefinition.tenant_id.is_(None),
        ).first() is not None

    was = _present()
    if not args.commit:
        print(f"(dry run) mobile_build node present={was}")
        return 0

    from utils.paid_import import optional_paid
    _smb = optional_paid("seed_mobile_builder")
    if _smb is None:
        print("SKIPPED: this edition does not seed the mobile builder node.")
        return 0
    created = _smb.seed_mobile_builder(session)  # idempotent: create/heal/no-op, self-commits

    # Best-effort keystore seed — never let it fail the node registration.
    try:
        from seed_mobile_builder import seed_mobile_keystore_secret
        ks = seed_mobile_keystore_secret(session)
        print(f"keystore secret seed: {ks}")
    except Exception as e:  # noqa: BLE001
        print(f"keystore secret seed skipped (non-fatal): {e}")

    print(f"Applied. mobile_build node created={created}, present={_present()} (was={was})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
