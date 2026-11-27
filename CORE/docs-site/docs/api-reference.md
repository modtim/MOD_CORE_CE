---
sidebar_position: 4
title: API Reference
description: RESTful JSON API for MOD Platform — authentication, files, workflows, nodes, and account endpoints with copy-paste examples.
dashboard_route: '#settings'
---

# API Reference

MOD Platform exposes a RESTful JSON API. Everything the Dashboard and Workflow Builder do is built on these same endpoints, so anything you can do in the UI you can automate.

- **Base URL:** `https://api.<your-domain>` for a hosted install; a local CE install serves the API at `http://localhost:8000` (the CE API port named in the README). Every route on this page is mounted under `/v1`.
- **Content type:** `application/json` for request/response bodies (file uploads use `multipart/form-data`)
- **Interactive docs:** the full, always-current OpenAPI/Swagger spec lives at **`/docs`** (e.g. `http://localhost:8000/docs`) and the raw schema at `/openapi.json`. When this page and `/docs` disagree, `/docs` wins.

## Authentication

Every endpoint (except a few public ones) requires an **OAuth2 Bearer token** in the `Authorization` header. There are two ways to get one.

### Option A — API key (recommended for scripts & integrations)

Create a key in **Dashboard → Gearworks - Settings → Gearworks - API Keys**. The full key (prefix `sk_`) is shown **once** at creation — store it securely. A key inherits the permissions of the user who created it.

An API key is **not** sent on every request. Instead you **exchange it once** for a short-lived Bearer token at the public `POST /v1/api-keys/token` endpoint, then send that token:

```bash
BASE=http://localhost:8000

# Exchange your sk_ key for a ~1h Bearer token (no auth header needed on this call)
TOKEN=$(curl -s -X POST "$BASE/v1/api-keys/token" \
  -H "Content-Type: application/json" \
  -d '{"api_key": "sk_xxxxxxxxxxxxxxxxxxxxxxxx"}' | jq -r .access_token)

# Use it on any endpoint
curl -s "$BASE/v1/me" -H "Authorization: Bearer $TOKEN"
```

The exchange returns `{ "access_token", "token_type": "Bearer", "expires_in", "scope" }`. When the token expires (`expires_in` seconds, ~1 hour), just call `/v1/api-keys/token` again.

### Option B — Bearer token (OAuth2 / Keycloak)

Tokens are issued by Keycloak and expire after ~1 hour. This is what the Dashboard uses. Obtain one with the OAuth2 authorization-code grant against the realm, then send it the same way: `-H "Authorization: Bearer <access_token>"`.

Refresh an expired token (Dashboard BFF flow):

```bash
curl -s -X POST "$BASE/v1/auth/refresh-token" \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "<refresh_token>"}'
```

If the header is missing or invalid you get `401`:

```json
{ "detail": "Authentication required. Provide either Bearer token or X-API-Key header." }
```

:::tip Try it live in the API explorer
The API is FastAPI, so the **interactive Swagger UI at [`/docs`](http://localhost:8000/docs)** lets you run any endpoint from your browser:

1. Open **[`http://localhost:8000/docs`](http://localhost:8000/docs)**
2. Click **Authorize** (top-right) and provide your **Bearer token** (the `access_token` from the exchange above).
3. Expand any endpoint → **Try it out** → fill the params → **Execute**. You'll see the exact `curl`, request URL, and live response.

The deep-links below (▶ **Open in explorer**) jump straight to each section.
:::

## Quick start

```bash
BASE=http://localhost:8000

# 0. Exchange your API key for a Bearer token (see Authentication above)
TOKEN=$(curl -s -X POST "$BASE/v1/api-keys/token" \
  -H "Content-Type: application/json" \
  -d '{"api_key": "sk_xxxxxxxxxxxxxxxxxxxxxxxx"}' | jq -r .access_token)
AUTH="Authorization: Bearer $TOKEN"

# 1. Who am I?
curl -s "$BASE/v1/me" -H "$AUTH"

# 2. Upload a file
curl -s -X POST "$BASE/v1/assets/upload-file" -H "$AUTH" \
  -F "file=@./input.png"

# 3. List your files
curl -s "$BASE/v1/assets/" -H "$AUTH"
```

> The examples below reuse `$BASE` and `$AUTH` from this Quick start block.

## Conventions

| Topic | Detail |
|-------|--------|
| **Pagination** | List endpoints accept `?offset=` and `?limit=` (default limit 50). |
| **IDs** | Integer primary keys unless noted. |
| **Timestamps** | ISO‑8601 UTC, e.g. `2026-06-23T13:25:24Z`. |
| **Multi-tenancy** | Every request is scoped to the caller's tenant automatically. |
| **Idempotency** | Re-sharing or re-granting a permission upserts rather than erroring. |

### Errors

Errors return a JSON body with a `detail` field and the appropriate status code:

| Status | Meaning |
|--------|---------|
| `400` | Bad request / invalid value |
| `401` | Missing or invalid credentials |
| `403` | Authenticated but not permitted |
| `404` | Not found (or not visible to your tenant) |
| `409` | Conflict (e.g. duplicate name — body includes a `code` such as `name_collision`) |
| `422` | Validation error (Pydantic field-level errors) |
| `500` | Server error — includes an `error_id` for support |

```json
{ "detail": "You don't have permission to share this item" }
```

## Account

▶ [Open in explorer](http://localhost:8000/docs#/Users)

```text
GET  /v1/me                  # current user profile
```

```bash
curl -s "$BASE/v1/me" -H "$AUTH"
```

```json
{
  "id": 3,
  "email": "you@example.com",
  "first_name": "Ada",
  "last_name": "Lovelace",
  "tenant_id": 1,
  "is_platform_admin": false,
  "onboarding_complete": true
}
```

## Files (Cogs)

▶ [Open in explorer](http://localhost:8000/docs#/Cogs)

In MOD, files and folders are **cogs**. A cog is one of `master_folder` (the tenant root, "My Files"), `asset_folder`, or `file`.

```text
GET    /v1/assets/?parent_id={id}        # list children of a folder (omit parent_id for the root)
GET    /v1/assets/root                   # get-or-create the tenant root folder
POST   /v1/assets/                       # create a folder  {name, cog_type, parent_id?}
POST   /v1/assets/upload-file            # upload a file (multipart)
GET    /v1/assets/{id}                   # cog details
GET    /v1/assets/{id}/download          # download the file bytes
PATCH  /v1/assets/{id}                   # rename / move  {name?, parent_id?}
DELETE /v1/assets/{id}                   # soft-delete (also removes its permissions)
```

**Create a folder:**

```bash
curl -s -X POST "$BASE/v1/assets/" -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{"name": "Project A", "cog_type": "asset_folder", "parent_id": 19}'
```

**Upload a file (simple, under ~100 MB):**

```bash
curl -s -X POST "$BASE/v1/assets/upload-file" -H "$AUTH" \
  -F "parent_id=19" \
  -F "file=@./render.exr"
# 201 Created -> the new cog
```

**Upload a large file (multipart):** big files use a chunked, presigned multipart upload — initiate, `PUT` each part to its presigned URL, then confirm with the part ETags:

```text
POST /v1/assets/uploads/initiate    # -> upload_id + presigned URL per part (64 MB default chunk)
PUT  <presigned part URL>      # upload each chunk directly to object storage (no auth header)
POST /v1/assets/confirm-upload      # parent_id, upload_id, s3_upload_id, parts_json (the ETags), file_size
```

The Dashboard's uploader does this for you; if you're scripting it, see **`/docs`** for the exact `initiate`/`confirm-upload` parameters (part size, `parts_json` shape). For most automation the simple `POST /v1/assets/upload-file` above is all you need.

**Download:**

```bash
curl -s "$BASE/v1/assets/123/download" -H "$AUTH" -o out.exr
```

## Permissions & sharing

▶ [Open in explorer](http://localhost:8000/docs#/Permissions)

Grant another user access to a file or folder. Folder grants cascade to everything inside; a direct grant on a file overrides what it inherited.

```text
GET    /v1/permissions/cogs/{id}?include_inherited=true   # who has access
POST   /v1/permissions/cogs/{id}                          # grant
PATCH  /v1/permissions/{permission_id}                    # change level
DELETE /v1/permissions/{permission_id}                    # revoke
```

```bash
curl -s -X POST "$BASE/v1/permissions/cogs/123" -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{"target_type": "cog", "target_id": 123, "user_id": 5, "level": "editor"}'
```

`level` is `viewer`, `editor`, or `owner`. Optional `expires_at` (ISO‑8601) sets an expiry; optional capability overrides (`can_upload`, `can_share`, `can_delete`, …) tune a level.

## Workflows

▶ [Open in explorer](http://localhost:8000/docs#/Workflows)

A **workflow** is a versioned DAG of nodes. You run a specific **version**; each run is tracked as a **workflow run**.

All workflow routes live under the `/v1/road` prefix:

```text
GET   /v1/road/                                          # list workflows
POST  /v1/road/                                          # create a workflow
GET   /v1/road/workflow-versions/{version_id}/variables    # which extra_variables this version accepts
POST  /v1/road/workflow-versions/{version_id}/run          # execute a version  -> 202 Accepted
GET   /v1/road/workflow-runs/{run_id}                       # run status + results
GET   /v1/road/workflow-runs/{run_id}/progress             # live progress detail
POST  /v1/road/workflow-runs/{run_id}/cancel               # cancel a run
POST  /v1/road/workflow-runs/{run_id}/restart              # restart a run
```

**Run a workflow version and poll for the result.** The body's `cog_ids` are the input files; `extra_variables` overrides node values at runtime. The variable names are workflow-specific — call the `variables` endpoint to see exactly which keys this version accepts:

```bash
# Discover the accepted variables (each item has a "variable_name" to use in extra_variables)
curl -s "$BASE/v1/road/workflow-versions/42/variables" -H "$AUTH"

# Kick off the run (202 Accepted -> { "workflow_run_id": ..., "cog_ids": [...] })
RUN=$(curl -s -X POST "$BASE/v1/road/workflow-versions/42/run" -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{"cog_ids": [123], "extra_variables": {"text_content": "a red sports car"}}' \
  | jq -r .workflow_run_id)

# Poll until the status is terminal
curl -s "$BASE/v1/road/workflow-runs/$RUN" -H "$AUTH" | jq '{status}'
```

A run's `status` moves through `queued` and `running` and ends at `completed` or `failed`. A cancelled run also ends as `failed`. The run object carries `status`, `run_files`, `node_runs`, and `error_message` (there is **no** `progress` field on it — use the dedicated `/v1/road/workflow-runs/{run_id}/progress` endpoint, which returns `progress_percentage`, `completed_nodes`/`total_nodes`, and `current_node`, for live detail).

## Node definitions

▶ [Open in explorer](http://localhost:8000/docs#/nodes)

Nodes are the building blocks of workflows; each references a containerized capability.

```text
GET   /v1/nodes/                             # list available nodes
GET   /v1/nodes/{id}                         # node schema (inputs/outputs/params)
POST  /v1/nodes/                             # register a custom node
```

## API keys

▶ [Open in explorer](http://localhost:8000/docs#/API%20Keys)

```text
POST   /v1/api-keys/token        # PUBLIC: exchange an sk_ key for a Bearer token
GET    /v1/api-keys/             # list your keys (prefix only, never the secret)
POST   /v1/api-keys/             # create  {description}  -> returns the full key ONCE
POST   /v1/api-keys/{id}/rotate  # mints a new key (new id, new secret); the old id and secret stop working
DELETE /v1/api-keys/{id}       # revoke
```

Create a key (the secret is returned **once**, in the `access_key` field):

```bash
curl -s -X POST "$BASE/v1/api-keys/" -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{"description": "CI pipeline"}'
# -> { "id": 7, "key_prefix": "sk_a1b2", "access_key": "sk_xxxx...", ... }   (access_key shown once)
```

Optional fields on create:

- `expires_at` — a date-time after which the key stops working. Leave it out for a key with no expiry. Community Edition enforces no maximum key lifetime.
- `capability_scope` — a list of scope names that limits the key. Omit it for a full-access key. The scope names this build recognises are `node:author`, `workflow:run`, `workflow:author`, `project:author`, `stack:author` and `automation:manage`.

Rotate a key (same description and scopes, new secret):

```bash
curl -s -X POST "$BASE/v1/api-keys/7/rotate" -H "$AUTH"
# -> 201 Created: { "id": <new id>, "access_key": "sk_yyyy...", "old_key_id": 7, "grace_until": null, ... }   (access_key shown once)
```

The new key keeps the old key's scopes and expiry. The old key stops working as soon as the rotation completes. Only the key's owner or an administrator may rotate it; other callers in the same tenant get `403`; a key in another tenant returns `404`. The call returns `404` for an unknown key, and `409` for a disabled key or a stack-pinned key (create a new stack key instead). If the platform re-auth setting is on and the call is made with a signed-in user token, the call returns `401` with `reauth_required` until you sign in again.

Then exchange that `access_key` for a Bearer token whenever you need to call the API — see [Authentication](#authentication). The first key must be created in the Dashboard (you need a token to call this endpoint, and the token comes from a key).

## Other resources

These follow the same patterns; see `/docs` for full schemas:

- `GET/POST /v1/road/triggers/` — schedule or webhook triggers that launch workflows
- `GET/POST /v1/workflow-templates` — reusable workflow starting points
- `GET/POST /v1/secrets/` — tenant secrets (write-only values)
- `GET/POST /v1/workers` — worker enrollment and status

## Calling a stack service through MOD

Identity-style request headers (`x-user*`, `x-tenant*`, `x-auth*`, `x-forwarded-*`, `x-mod-*`, `x-actor*`, `x-caller*`, `x-owner*`, `x-permission*`, `x-uid*`, `x-sub*`, `x-whoami*` and similar) sent by callers are removed before MOD forwards a stack call; use an app-prefixed header name for your own tokens (for example `X-<yourapp>-Token`). Identity of the caller, when enabled by the platform setting `stacks.call_identity_claim_enabled` (default off), is in the signed stack-call token (the `Authorization: Bearer` MOD adds), not in headers.

## Rate limits

| Surface | Limit |
|---------|-------|
| Default | Unlimited (Community Edition applies no API rate limit) |
| Per API key | Unlimited (an operator can set a cap in Settings: `api.rate_limit.per_key_per_minute`) |
| Worker heartbeat | 30 requests / minute / client IP |

Exceeding an operator-set limit, or the worker-heartbeat limit, returns `429 Too Many Requests`. Back off and retry with jitter.

---

**Tip:** the Dashboard is the fastest way to discover request/response shapes — open your browser's Network tab while clicking around, and you'll see the exact calls. For the authoritative, generated schema, always check **`/docs`**.
