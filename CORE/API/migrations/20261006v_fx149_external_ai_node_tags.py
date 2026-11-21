"""FX.149 part 2b (owner decision Q136=A) — seed external-AI node tags.

Applies the ``external-ai`` tag from
docs/security-audit/external-ai-provider-inventory.md (row 6: NIM Bridge
workflow nodes calling NVIDIA NVCF hosted inference — tenant INPUT FILES
leave the box) to the matching SYSTEM node definitions. The tag lives in
the EXISTING ``custom_metadata`` JSONB under key ``tags`` (list of
strings) — the same soft-tag pattern as the FX.132 prohibited-use tags
(services/ai_prohibited_use.py): no new column, no models.py change.

At run start, a graph containing a tagged node is refused for tenants
that fail the external-LLM policy (services/external_ai_node_gate.py
check_run_external_ai — platform egress.external_llm_enabled/sealed_mode +
tenant opt-in ai.external_llm_requires_tenant_opt_in, fail-closed).

Only the "cloud" NIM node kinds are tagged: the *_local kinds run the
model on-box and never send tenant data to an external provider. Nodes
not present in this install are skipped silently (the inventory is a
proposal, the DB is the palette).

SOFT + idempotent: the script only ADDS tags (set-union merge); it never
removes tags, never deletes rows, and a second run is a no-op.

Dry run (default) prints what it WOULD write. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261006v_fx149_external_ai_node_tags.py            # dry
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261006v_fx149_external_ai_node_tags.py --commit   # apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# node_kind -> tags added (NIM Bridge "cloud" nodes: hosted NVCF inference,
# tenant input files base64-encoded out of the box).
_TAG_MAP = {
    "nim_trellis_cloud": ["external-ai"],
    "nim_studiovoice_cloud": ["external-ai"],
    "nim_eyecontact_cloud": ["external-ai"],
    "nim_lipsync_cloud": ["external-ai"],
    "nim_relighting_cloud": ["external-ai"],
    "nim_bnr_cloud": ["external-ai"],
    "nim_wan_cloud": ["external-ai"],
}


def merge_tags(kind: str, existing) -> list:
    """Pure, idempotent merge: existing tags + the proposed tags for this
    node_kind (set-union, order-preserving). Never removes anything. Exposed
    for tests (the live apply below uses the same function)."""
    existing = [t for t in existing if isinstance(t, str)] if isinstance(existing, list) else []
    return existing + [t for t in _TAG_MAP[kind] if t not in existing]


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()

    conn = _get_conn()
    cur = conn.cursor()
    kinds = list(_TAG_MAP)
    cur.execute(
        "SELECT id, node_kind, custom_metadata FROM node_definitions "
        "WHERE tenant_id IS NULL AND node_kind IN (%s)"
        % ",".join(["'%s'" % k for k in kinds])
    )
    rows = cur.fetchall()
    touched = 0
    for row in rows:
        nd_id, kind, cm = row[0], row[1], row[2]
        cm = cm if isinstance(cm, dict) else {}
        merged = merge_tags(kind, cm.get("tags"))
        current = cm.get("tags") if isinstance(cm.get("tags"), list) else []
        if merged == current:
            continue  # idempotent: already tagged
        new_cm = dict(cm)
        new_cm["tags"] = merged
        if args.commit:
            cur.execute(
                "UPDATE node_definitions "
                "SET custom_metadata = %s WHERE id = %s;",
                (json.dumps(new_cm), nd_id),
            )
        print(f"TAG  node {nd_id} ({kind}) += external-ai")
        touched += 1
    print(f"FX.149 part 2b external-AI tags: {touched} node definitions "
          f"{'updated' if args.commit else 'would be updated'} "
          f"({len(rows)} candidate rows scanned).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
