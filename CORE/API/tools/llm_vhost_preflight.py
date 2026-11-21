#!/usr/bin/env python3
"""remote-worker LLM-vhost preflight.

The managed-LLM nginx vhost is gated with a CIDR allowlist
(platform setting ``llm.vhost_allowed_cidrs``: tailnet 100.64.0.0/10 + the
LAN CIDRs). A remote (BYOH) worker that reaches the managed LLM over the
public internet — i.e. whose last-seen address is NOT on the tailnet and
not inside those LAN CIDRs — will lose that endpoint the moment the ACL
is applied. This tool lists those workers BEFORE the rollout so the
operator can move them onto the tailnet instead of discovering it later.

Run it INSIDE the API container (it uses the API's own database session
factory, never a test fixture), same seam as tools.drain_and_wait:

    docker exec my_fastapi_app python3 -m tools.llm_vhost_preflight

Exit codes (the installer's preflight vocabulary — WARN is non-fatal):
    0  ok      every remote worker's address is inside the allowlist
    3  warn    at least one remote worker is provably off-allowlist
    2  error   the check itself could not run (DB/settings unavailable)

It NEVER returns 2 for a warn and never blocks an install: the existing
preflight pattern (CORE/startup/scripts/preflight.sh) reserves rc 2 for a
BLOCK, so a warn uses rc 3 and the shell callers print WARN lines only.
"""
from __future__ import annotations

import os
import sys

# Make `services` / `database` importable when run as `python3 -m tools.*`
# from /app (= CORE/API) — the container's CWD; harmless if already on path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal  # noqa: E402
from utils.paid_import import optional_paid  # noqa: E402

# 1647c2 (CE boot): services.llm_vhost_nginx is denied in the CE carve while
# this tool ships in it, so the module-level import is resolved through
# optional_paid. On CE there is no managed-LLM vhost to preflight, so the tool
# reports the check as not run (rc 2, non-fatal to the installer).
lvn = optional_paid("services.llm_vhost_nginx")


def _report() -> int:
    if lvn is None:
        print("[preflight] ERROR: llm-vhost worker preflight could not run "
              "(llm-vhost module is not available in MOD "
              "Community Edition); not blocking the install")
        return 2
    db = SessionLocal()
    try:
        result = lvn.off_allowlist_workers(db=db)
    finally:
        db.close()
    for line in lvn.render_off_allowlist_warnings(result):
        print(line)
    status = result.get("status")
    if status == "error":
        print("[preflight] ERROR: llm-vhost worker preflight could not run "
              "(no DB/settings); not blocking the install")
        return 2
    if status == "warn":
        print(f"[preflight] WARN: {result['warned']} of {result['checked']} "
              "remote worker(s) are off the LLM vhost allowlist "
              f"({', '.join(result['allowed_cidrs'])})")
        return 3
    print(f"[preflight] ok: all {result['checked']} remote worker(s) are on "
          "the tailnet or an allowed LAN CIDR")
    return 0


def main(argv=None) -> int:
    return _report()


if __name__ == "__main__":
    raise SystemExit(main())
