"""2400GX-c3 J3 (30.6.3) — per-stack deploy tokens, merged into API keys (c3-J4 TOKMERGE).

A stack-pinned key is an ordinary `ApiKey` row with `deploy_token_stack_id`
set (c3-J1, models.py:956) and its chosen scopes stored in `capability_scope`
as `stack:<scope>` (API_KEY_DEPLOY_TOKEN_SCOPES, models.py:864). Such keys are
minted ONLY via `POST /v1/api-keys` (c3-J2, routers/api_keys.py) and revoked
via `DELETE /v1/api-keys/{id}` (soft delete); there is no separate deploy-token
mint/list/revoke route or service path any more — the c3-J3 router
`routers/persistent_stacks/deploy_tokens.py` was deleted by c3-J4 and the
mint/list/revoke service code here is gone. This module now keeps only the
platform-settings names the merged mint path reads and a live-pin counter.

Security MUSTs (design §4, Collective c3):
- S1 the tenant comes from the minting human plus the stack row — the body
  never names a tenant;
- S2 stack-admin only: the ROUTER owned that (the router-wide
  `require_platform_capability(STACK_AUTHOR)` gate, __init__.py:58, plus
  `_get_owned` from `routers/persistent_stacks/_shared.py:2128`); the merged
  mint path owns its own auth posture in routers/api_keys.py;
- S6 reveal-once: the plaintext exists only in the mint response, hashed with
  `security.hash_api_key` — the exact helper `routers/api_keys.py:105` uses —
  and never appears in list rows, audits or logs;
- S7 revoke is soft-delete only (SoftDeleteMixin `deleted_at`, the same
  mechanism `crud/api_key_cud.py:revoke_api_key` uses); no hard delete;
- S15 a deploy token cannot mint/list/revoke — c3-J2's funnel scope check
  (services/app_key_scope.py) denies these routes for pinned keys.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

import models

# The scopes the Collective says are NOT in the first ship (Lead vet,
# c3-design.md:120) — the merged mint path refuses them with its own code.
_UNSUPPORTED_SCOPES = frozenset({"secrets:write", "workflows:apply"})

# The platform-settings keys the merged mint path reads (routers/api_keys.py
# keeps its own string literals at :110 :150 :151 :171 — do NOT edit that file
# from here). SAME string values as the old private `_SETTINGS_*` names; the
# settings themselves are registered in services/platform_settings.py:2523 and
# must never be renamed (existing tenant rows are keyed on them).
SETTING_ENABLED = "stacks.deploy_token_enabled"
SETTING_TTL_DEFAULT = "stacks.deploy_token_ttl_days_default"
SETTING_TTL_MAX = "stacks.deploy_token_ttl_days_max"
SETTING_MAX_PER_STACK = "stacks.deploy_token_max_per_stack"


def live_pinned_key_count(db: Session, stack_id: int) -> int:
    """Live (not soft-deleted) stack-pinned ApiKey rows pinned to `stack_id`.

    Tenant isolation: this is a PURE COUNT by stack id — it adds no data path
    and no route. Trusted callers (the deploy gate, the merged mint limit)
    resolve the stack through `get_owned_stack` / `_get_owned` BEFORE calling,
    so the stack id is already tenant-scoped; the count itself names no
    tenant and returns no row data. Same filter the old `_live_tokens` helper
    used (stack_deploy_policy's caller at persistent_stacks_domain_service.py
    :487 is J6's to retarget).
    """
    return db.query(models.ApiKey).filter(
        models.ApiKey.deploy_token_stack_id == stack_id,
        models.ApiKey.deleted_at.is_(None)).count()
