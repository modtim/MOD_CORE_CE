# API conventions + lint gate (scaffold)

Part of openspec change `stack-training-and-prewarm`, Workstream B (API refactor),
Gate 0, task 1.5. This directory holds the **convention lint gate**; the
conventions it enforces live in `api_conventions.py` and `services/_base.py`.

Everything here is **scaffold** — it defines the target pattern the per-domain
migration (Gate 2) builds on. Nothing is wired into `main.py` or CI yet, and no
existing router has been changed.

## Why

The deep audit (`docs/api-audit/deep/`) found the structural root cause of the
API's recurring bug classes: **~100 of 141 router files do inline
`db.query(models.X)`**, bypassing the service/CRUD layer. Because every router
hand-writes its own data access, the same defects were written over and over — 3
cartesian queries, 54 N+1s, 61 unbounded list endpoints, 40 external-calls-in-a-
DB-session, and 1,756 ad-hoc `HTTPException` shapes. Fixing the pattern in ONE
place per domain (design.md **D9**) is what makes the domain reshuffle (**D5**)
worth doing; a canonical cross-cutting layer (**D6**) removes the per-router
inconsistency; a lint gate (**D8**) stops it regrowing.

## The canonical conventions

### 1. Cross-cutting module — `api_conventions.py`

| Concern | Use | Notes |
|---|---|---|
| **Auth + tenant + permission** | `access(resource_type, capability)` → `AccessContext`; `authenticated_user()`; `require_access(...)` | Thin composition over the EXISTING `dependencies.py` (keycloak_auth) and `permissions.py`/`resource_permissions.authorize`. Does **not** reinvent auth. `AccessContext` exposes `.user`, `.tenant_id`, `.user_id`. |
| **Errors** | `ErrorEnvelope` / `error_response(...)` / `install_error_handlers(app)` | Canonical body `{"error": {code, message, details, request_id}}`. Handlers normalise any `HTTPException` (str or dict `detail`) — routers keep raising plain `HTTPException`. |
| **Pagination** | `pagination` dependency → `Pagination`; `Page[T]` response envelope | `limit`/`offset` (the dominant existing style), bounded by `Query(ge=1, le=200)`. Every list endpoint takes `Pagination` and returns `Page.build(items, total, page)`. |

### 2. Data-access layer — `services/_base.py`

Layering, top calls down, never the reverse:

```
routers/<domain>.py    HTTP only — auth, validate, shape response. NO db.query.
  -> services/<domain>.py    business logic; owns the txn; external I/O OUT of the session.
    -> crud/<domain>_cud.py  the ONLY place db.query(models.X) lives for the domain.
      -> models.py           ORM.
```

`CRUDBase` is an optional convenience for the common single-model, tenant-scoped
case (tenant filter + soft-delete filter + `selectinload` + bounded `list()`
returning `(rows, total)`). Domains with richer needs write plain functions in
`crud/<domain>_cud.py` (matching e.g. `crud/tag_cud.py`) — the **rules** are the
contract, `CRUDBase` is just one way to meet them:

- routers contain no `db.query(`;
- list reads are bounded (take `Pagination`, return slice + total);
- collections use `selectinload`, never two `joinedload` collections in one query;
- no blocking external call while a DB `Session` is open;
- tenant-scoped queries filter on `tenant_id`;
- soft-delete only (ISO 27001, no hard deletes).

Services raise `ServiceError` / `NotFoundError` / `ConflictError`; the **router**
maps those to HTTP. HTTP concerns stay out of the service layer so services are
reusable by background jobs.

### 3. Worked reference — `services/_reference_domain_example.py`

A full `router → service → crud → models` example for the **notifications**
domain, re-implementing `GET /notifications/list` (today an inline-`db.query`,
hand-clamped, bare-dict handler) the canonical way. It is a **template only** —
not imported by `main.py`; the real `routers/notifications.py` is untouched.

## The lint gate — `api_lint.py`

Static AST scan of `routers/`. **Report-only / non-failing by default.**

| Rule | Checks |
|---|---|
| R1 | Max endpoints per router file (`--max-endpoints`, default 40) |
| R2 | Every endpoint has an auth dependency (route-level `Depends`, param `Depends`, or router-level `dependencies=`) |
| R3 | Every endpoint declares a `response_model` |
| R4 | No duplicate `(METHOD, path)` registration (candidate — see limitation) |
| R5 | Routers contain no raw `db.query(` |

```bash
# from CORE/API (inside my_fastapi_app: cd /app)
python -m tools.api_lint --report            # human summary, exit 0
python -m tools.api_lint --report --json      # machine-readable
python -m tools.api_lint --report --only R4,R5
python -m tools.api_lint --strict             # exit 1 on any finding (future CI)
```

**Known skeleton limitations (by design):**

- **R4 prefixes** — resolves `APIRouter(prefix=...)` but NOT prefixes applied at
  `include_router(prefix=...)` in `main.py`. Cross-file R4 hits are *candidates to
  confirm* (this is how the audit surfaced the real duplicate
  `DELETE /resource-permissions/{id}`), not certainties.
- **R2 heuristic** — auth is recognised by dependency name (`require_*`,
  `*current_user*`, `*api_key*`, `*_token*`, `authenticated_*`, `access`).
  Public-by-design endpoints appear as R2 findings and get **triaged**, not
  auto-failed.
- Pure AST — the scan never imports routers (no DB, no side effects). A file that
  won't parse is reported (`R0`), never a crash.

## How this gets enforced later (D8, Gate 3)

1. **Now (Gate 0):** scaffold only. Run `--report` to see the current baseline.
2. **Gate 2:** per-domain agents migrate routers onto these conventions; each
   domain's findings drop to zero as it lands.
3. **Gate 3:** wire `python -m tools.api_lint --strict` into CI. Start scoped to
   **new/changed** files (so the gate blocks regressions without requiring the
   whole surface to be clean at once), then tighten to the full tree as the
   backlog clears. R1/R4 thresholds are tuned from the migrated baseline.
