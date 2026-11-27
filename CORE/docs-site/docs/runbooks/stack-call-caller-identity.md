---
sidebar_position: 53
slug: /runbooks/stack-call-caller-identity
title: Caller identity and on-behalf-of for a stack
description: "Runbook: a self-hosted MOD install lets a stack learn WHO is calling it through signed stack-call tokens (caller identity) and, when the tenant admin enables it, start a workflow run acting for that caller (on-behalf-of). How the stack verifies the tokens, what the deny list protects against, and the failure codes."
---

# Caller identity and on-behalf-of for a stack

Last reviewed: 2026-11-25

A **stack** is a user-facing app (agent, workflow builder, data processor, any self-hosted service). When a tenant user calls a stack through MOD, MOD can attach a **caller-identity token** to the `Authorization: Bearer` header so the stack knows **which user** reached it. When the tenant admin enables **on-behalf-of** for that stack, the stack may also **start a workflow run** on behalf of that user — the stack acts for the user, but only with the user's own permissions, only on a pre-approved list of workflow IDs, and every use is audited.

This page covers:

- **Caller identity**: the stack-call token the end user already carries (slice 1)
- **On-behalf-of (OBO)**: the new `mod-stack-obo-v1` token the stack sends to MOD to start a run (slice 4)
- **Header-spoof protection**: which headers are dropped before the token reaches the stack
- **Verification examples** (Python, Node)
- **Failure codes**

> **WARNING — Planned content**: Sections 4–6 describe slices not yet built (per-stack opt-in switch, the OBO endpoint migration, the OBO run-start endpoint). They are marked `PLANNED` and the stack can only verify what exists today (the `Authorization: Bearer` token).

---

## 1. What the stack-call token carries today

MOD's stack-call proxy (the route `POST /v1/stacks/{id}/services/{svc}/call/{path}`) already mints a JWT as `Authorization: Bearer <token>` and forwards it to the stack. The token type is `mod-trigger-call-v1`.

When the platform setting `stacks.call_identity_claim_enabled` is **on**, the token also carries:

| Claim | Meaning |
|---|---|
| `sub` | `"user:<user-id>"` — the user who called. For an API key: the key owner. |
| `sub_kind` | `user` / `api_key` / `service_account` |
| `via` | `interactive` / `programmatic` / `stack_identity` |
| `scope` | `caller.identity` |

> **TTL**: today 300 s. After slice 1 ships the default becomes 120 s.

The stack **must not** trust any other header it received to be the user — only the signed token is the source of truth. See §3.

---

## 2. On-behalf-of (OBO) — what it is and when to enable

> **Planned** — not yet built. See slice 4.

When enabled for a stack, MOD additionally sends a second token on the `X-MOD-On-Behalf-Of` header. This token (`mod-stack-obo-v1`) allows the stack to call back MOD and **start a workflow run** as the user, subject to:

1. **User permissions only** — the run runs with exactly the user's permissions, no more.
2. **Allowlist** — only workflow IDs the tenant admin has added to the stack's `allowed_workflow_ids` list.
3. **Single-use + replay protection** — each `jti` is accepted once; a replay returns the same run id, never a new one.
4. **Second factor** — the stack must present its own `mod-stack-identity-v1` token to MOD, so a stolen OBO token alone is useless.
5. **120 s hard TTL** — expires after 2 minutes.

**When to enable OBO**: when the stack needs to persist or process the user's call result (e.g. save it to a database, trigger a downstream workflow) and the user cannot do that directly. It is **off by default** — the tenant admin enables it per stack in the admin UI.

> **CE note**: OBO is fully shared code. CE has no cap on the number of runs a user may start; the technical circuit-breakers (rate limits) are set high enough that no human use hits them.

---

## 3. Header-spoof protection — what the stack can and cannot trust

MOD strips certain headers before forwarding to the stack. A stack that trusts those headers is spoofable.

### What MOD strips (always)

| Prefix / name | Dropped? | Example |
|---|---|---|
| `x-api-key` | ✅ | `x-api-key: secret` |
| `x-real-ip` | ✅ | `x-real-ip: 10.0.0.1` |
| `x-mod-*` | ✅ | `x-mod-user-id`, `x-mod-role` |
| `x-forwarded-*` | ✅ | `x-forwarded-for` |
| `authorization` | ✅ (overwritten by MOD's Bearer) | — |
| `cookie` | ✅ | — |

### When `stacks.call_identity_claim_enabled` is ON — also strip these

| Pattern | Reason |
|---|---|
| `x-user*` | `x-user-id`, `x-user-email` |
| `x-auth*` | `x-auth-user`, `x-authenticated` |
| `x-remote-user` | Apache-style identity header |
| `x-forwarded-user` | Proxy identity leak |
| `x-end-user*` | `x-end-user-id` |

> **Planned** — this deny list is implemented in slice 1. Today (before slice 1 ships), `x-user-*` and `x-auth-*` pass through unchanged. A stack that trusts them is spoofable.

### What the stack can trust

- **Only** the JWT in `Authorization: Bearer` (caller identity) and `X-MOD-On-Behalf-Of` (OBO, when enabled).
- The token is RS256-signed; verify it against the JWKS at `GET {MOD_BASE}/v1/.well-known/jwks.json`.
- Never trust `x-user-*`, `x-auth-*`, `x-forwarded-user`, `x-end-user-*` — they are user-provided and can be set by any caller to impersonate.

---

## 4. Verification — worked examples

> **UNTESTED** — these snippets are provided for the stack author. They have not been run against the live MOD API in this build.

### 4a. Caller identity token — Python

```python
import jwt
import requests
from datetime import datetime, timezone

MOD_BASE = "https://mod.example.com"  # your install's base URL

def verify_caller_identity(authorization: str) -> dict | None:
    """Verify the stack-call Bearer token. Returns claims dict or None on failure."""
    try:
        # Fetch the JWKS from MOD — always the same for this install
        jwks = requests.get(f"{MOD_BASE}/v1/.well-known/jwks.json", timeout=10).json()
        signing_key = jwt.get_unverified_header(authorization.split()[1])["kid"]
        key = next((k for k in jwks["keys"] if k["kid"] == signing_key), None)
        if not key:
            return None  # unknown kid

        payload = jwt.decode(
            authorization.split()[1],
            key=key,
            algorithms=["RS256"],
            audience=f"stack:<your_stack_id>",  # replace with your stack's ID
            issuer="mod-trigger",
        )

        # Check scope — caller identity only
        if payload.get("scope") != "caller.identity":
            return None

        # Check expiry
        if datetime.now(timezone.utc).timestamp() > payload.get("exp", 0):
            return None

        # Replay detection (the stack should keep a seen-jti set, TTL'd to exp)
        jti = payload.get("jti")
        if jti and jti in _seen_jtis.get("call_identity", []):
            return None  # replay

        return {
            "user_id": payload["sub"].removeprefix("user:"),
            "sub_kind": payload.get("sub_kind"),
            "via": payload.get("via"),
            "tenant_id": payload["tenant_id"],
        }

    except Exception:
        return None  # any failure -> unauthenticated


# Usage in a stack endpoint:
# def my_endpoint(req):
#     identity = verify_caller_identity(req.headers.get("Authorization", ""))
#     if not identity:
#         return 403, "unauthorized"
#     # identity["user_id"] is the caller
```

### 4b. OBO token — Python (slice 4)

> **Planned** — `mod-stack-obo-v1` not yet minted in this build.

```python
def verify_obo(x_mod_header: str) -> dict | None:
    """Verify the on-behalf-of token on the X-MOD-On-Behalf-Of header."""
    try:
        jwks = requests.get(f"{MOD_BASE}/v1/.well-known/jwks.json", timeout=10).json()
        signing_key = jwt.get_unverified_header(x_mod_header.split()[1])["kid"]
        key = next((k for k in jwks["keys"] if k["kid"] == signing_key), None)
        if not key:
            return None

        payload = jwt.decode(
            x_mod_header.split()[1],
            key=key,
            algorithms=["RS256"],
            audience=f"stack-obo:<your_stack_id>",  # NOTE: "stack-obo:" prefix
            issuer="mod-stack-call",
        )

        # Only the run:start scope is valid
        if "run:start" not in (payload.get("scope") or "").split():
            return None

        # TTL hard cap 120 s
        if payload.get("exp", 0) - payload.get("iat", 0) > 120:
            return None

        return {
            "user_id": payload["sub"].removeprefix("user:"),
            "stack_id": payload.get("stack_id"),
            "workflow_allowlist_check": True,  # caller must validate allowlist in DB
        }

    except Exception:
        return None
```

### 4c. Node.js — caller identity

```javascript
const jwt = require('jsonwebtoken');

async function verifyCallerIdentity(authHeader) {
  // Fetch JWKS
  const resp = await fetch(`${MOD_BASE}/v1/.well-known/jwks.json`);
  const jwks = await resp.json();

  const header = jwt.decode(authHeader.split(' ')[1], { complete: true });
  const kid = header.header.kid;
  const key = jwks.keys.find(k => k.kid === kid);
  if (!key) return null;

  // Convert JWK to PEM (use a package like jwk-to-pem or node-jose)
  const pem = jwkToPem(key);  // ← use a preferred JWK→PEM tool

  return jwt.verify(authHeader.split(' ')[1], pem, {
    audience: `stack:${YOUR_STACK_ID}`,
    issuer: 'mod-trigger',
  });
}
```

### 4d. Node.js — OBO (slice 4)

```javascript
async function verifyObo(xModHeader) {
  const resp = await fetch(`${MOD_BASE}/v1/.well-known/jwks.json`);
  const jwks = await resp.json();

  const header = jwt.decode(xModHeader.split(' ')[1], { complete: true });
  const kid = header.header.kid;
  const key = jwks.keys.find(k => k.kid === kid);
  if (!key) return null;

  const pem = jwkToPem(key);

  return jwt.verify(xModHeader.split(' ')[1], pem, {
    audience: `stack-obo:${YOUR_STACK_ID}`,
    issuer: 'mod-stack-call',
  });
}
```

---

## 5. Failure codes

| Code | Meaning | Stack action |
|---|---|---|
| `401` | Token expired, tampered, wrong audience, wrong type, unknown kid | Reject the call |
| `401` | OBO: missing stack's own identity token | Reject (second factor required) |
| `403` | OBO: `obo_not_enabled` (stack's admin disabled it) | Reject; inform tenant admin |
| `403` | OBO: `workflow_not_allowed` (workflow not in allowlist) | Reject; inform the user |
| `403` | OBO: `subject_not_eligible` (service-account caller) | Reject |
| `403` | OBO: `user_not_permitted` (user deactivated / API key expired) | Reject; inform the user |
| `404` | OBO: workflow version not in this tenant | Reject |
| `409` | OBO: idempotency key conflict (same jti, different body) | Retry with new jti |
| `422` | OBO: missing `idempotency_key` in request | Fix the request |
| `429` | OBO: rate limit exceeded | Retry after back-off |
| `503` | MOD: dispatch paused (system maintenance) | Retry after back-off |

---

## 6. Security rules — never trust headers

**Rule 1 — Never trust any header for identity.** Headers (`x-user-id`, `x-auth-*`, `x-forwarded-user`) are user-controlled and can be set by any caller. They are **not** a source of truth.

**Rule 2 — Only the signed JWT is the source of truth.** Verify it against the JWKS, check `aud`, `iss`, `exp`, `scope`, and `sub`. Reject on any failure.

**Rule 3 — Reject replay.** Keep a seen `jti` set (TTL = token lifetime). A replayed token is the same run, never a new one.

**Rule 4 — Never log token contents.** Tokens carry user IDs and tenant IDs. Log only `jti` (a random uuid) and `stack_id`.

**Rule 5 — OBO needs a second factor.** The stack must also present its own `mod-stack-identity-v1` token. A stolen OBO token from logs alone is useless.

---

## 7. Planned — per-stack opt-in (slice 2)

> **PLANNED** — not yet built in this build.

When slice 2 ships, the tenant admin will be able to enable caller identity per stack:

1. **Admin UI** — a checkbox on the stack's settings page: `Enable caller identity` and `Enable on-behalf-of`.
2. **When on**: MOD adds `sub`, `sub_kind`, `via`, `scope` to the `Authorization: Bearer` token.
3. **When OBO on**: MOD adds the `X-MOD-On-Behalf-Of` header (only when the caller is `sub_kind=user` or `api_key`, never `service`).
4. **Allowlist** — the admin lists workflow IDs the stack may start. Empty = delegation refused.

The admin endpoint is `PUT /v1/stacks/{id}/caller-identity`.

---

## 8. Planned — migration for run attribution (slice 3)

> **PLANNED** — not yet built.

When the OBO endpoint is live, each run will carry:

- `initiated_by_stack_id` (integer, nullable, no FK — a deleted stack does not cascade)
- `initiated_by_stack_service` (varchar)

This appears in the run-read schema as `initiated_by_stack: {stack_id, service} | null`.

---

## 9. Audit trail

Every OBO use — including refusals — writes an `AuditEvent(event_name="stack_obo_run")` with:

- `acting_user_id`, `stack_id`, `service`, `workflow_id`, `run_id` (null on refusal)
- `result`: `"started"`, `"replay"`, `"denied:<reason>"`, or `"error:<status>"`
- `caller_jti` — links back to the originating stack call

The stack-call audit row also carries `caller_jti` when slice 1 ships, so a call can be joined to its later OBO uses.

---

## 10. CE / Paid note

This feature is **shared code**. CE receives the full capability with no cap and no licence gate. The technical circuit-breakers (rate limits) are set high enough (600/min/stack, 120/min/user) that no human use hits them; they are a safeguard against buggy loops, not a quantity limit on runs a person may start.

No paid-only variant is planned for this slice. Future paid layers (admin approval per delegation grant, per-stack usage metering) would be separate.
