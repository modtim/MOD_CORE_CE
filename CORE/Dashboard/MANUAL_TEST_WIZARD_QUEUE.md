# Manual Test — Wizard Queue (Dashboard: System → Activity)

Feature: the unified `WizardRun` observational console. Every long-running
wizard (Node Wizard, Stack Wizard, AI Assistant Agent/Project, Build Repair,
Container Adapter) surfaces one row here, independent of its own native
execution path (design `wizard-queue-redesign`).

Backend: `CORE/API/routers/wizard_queue.py` (all under `/wizard-queue`,
served under `/v1`), `CORE/API/services/wizard_queue.py` (visibility/cancel/
retry logic), `CORE/API/services/wizard_queue_emitters.py` (per-wizard
surfacing). Dashboard: `CORE/Dashboard/js/wizard-queue.js`
(`initializeWizardQueuePanel` / `stopWizardQueuePanel`),
`CORE/Dashboard/pages/analytics.html` (`#main-tab-activity`,
`#main-panel-activity`, `#wizard-queue-root`),
`CORE/Dashboard/js/analytics.js` (tab wiring, ~L56/~L103-146/~L935-940).
WorkflowBuilder mirror (same contract, different renderer):
`CORE/WorkflowBuilder/lib/wizard-queue.ts`,
`components/{wizard-run-row,wizard-runs-section,build-queue-panel}.tsx`.

Endpoints exercised:
```
GET  /wizard-queue?limit=20&offset=0&status_filter=&wizard_type=
GET  /wizard-queue/{id}
GET  /wizard-queue/{id}/logs?after_seq=-1&limit=500
GET  /wizard-queue/{id}/logs/stream?access_token=...   (SSE)
GET  /wizard-queue/{id}/logs/export?format=json|csv
POST /wizard-queue/{id}/cancel
POST /wizard-queue/{id}/retry
POST /wizard-queue/{id}/dismiss
POST /wizard-queue/clear-completed
```

`PSQL=` shorthand:
```bash
docker exec my_postgres_db psql -U modTech -d modTech -c "<sql>"
```

## Prerequisites

- [ ] Full stack up (`start-main-server.sh`); `docker ps` shows
      `my_fastapi_app` and `mod_dashboard` healthy.
- [ ] Logged in to the Dashboard as a **tenant admin** (`admin`, no
      `parent_user_id`), a **sub-user** (a user WITH `parent_user_id` set, same
      tenant), and a **platform admin** — three separate sessions/tabs (or
      incognito windows) so the visibility matrix can be checked without
      logging in/out repeatedly.
- [ ] `sessionStorage` holds a valid `accessToken` for each session (the SSE
      tail authenticates via `?access_token=`, not a header — Dashboard uses
      `sessionStorage`, WB uses `localStorage`, per the E2E-auth-gotchas
      memory).

## 1. Golden path — every wizard type appears

- [ ] As `admin`, POST `/stacks/from-wizard` with
      `{"name":"test-stack","prompt":"redis cache"}` (via curl/Postman with the
      bearer token, or trigger it through the WorkflowBuilder Stack Wizard UI).
      This emits a **STACK** run directly via `wizard_queue.create_run()` in
      `routers/persistent_stacks/persistent_stacks.py:create_from_wizard`
      before the LLM call, then marks it SUCCEEDED/FAILED on return.
  - Expected: within a few seconds, Dashboard → **System** → **Activity** tab
    shows a row with type label "Stack Wizard", target = the first 60 chars of
    the prompt, and a terminal status badge (`data-status="succeeded"` or
    `"failed"`).
  - Verify (PSQL): `select wizard_type, status, target_name from wizard_runs order by created_at desc limit 5;`
- [ ] Trigger an **AGENT_PROJECT** run via AI Assistant → start an
      autonomous project/agent session.
  - Expected: an "Agent / Project" row appears at `running` immediately
    (`emit_agent_project_run` starts it RUNNING, not QUEUED). Its child node
    builds (if any) are separate rows linked via `parent_run_id` — they are
    NOT separately visible as top-level rows in the Dashboard panel filter
    unless the panel elects to show them (the WB panel explicitly filters
    `wizard_type === "node"` sub-rows out of "Other wizard runs"; the
    Dashboard panel shows all in-scope rows).
  - Verify: the row shows status `running` (not `queued`) and type label
    "Agent / Project".
- [ ] Trigger a **NODE** wizard build via WorkflowBuilder Node Wizard → build
      a node (Node Library → + New Node → "Build from scratch" or "Generate
      Dockerfile").
  - Expected: the row appears in the **Build Queue** panel (not in the
    "Other wizard runs" section), advancing `queued → waiting-for-worker →
    running → verifying → succeeded/failed` (see `_PROGRESS_FOR_STATUS` —
    the progress bar advances 5/10/50/85/100).
  - Verify: NODE runs do NOT appear in the Activity tab's "Other wizard runs"
    section — they are rendered exclusively by `BuildQueueRow`.
- [ ] Trigger a **CONTAINER_ADAPTER** run via Node Wizard → "Generate
      Dockerfile" (Node Library → + New Node → Generate Dockerfile).
      This dispatches via `routers/node_definitions_domains/node_authoring.py:
      generate_dockerfile` → `_enqueue_container_task` → `emit_container_task_run`.
  - Expected: a "Container Adapter" row appears in the Activity tab at
    `queued`, then advances through the lifecycle as the task executes.
  - Verify (PSQL): `select target_ref_type, target_ref_id from wizard_runs
    where wizard_type='container-adapter' order by created_at desc limit 1;`
    should show `task`/task-id linkage.
- [ ] Trigger a **BUILD_REPAIR** run via Node Wizard → repair a broken node's
      artifacts (`woodsman/wizard/repair-artifacts`).
  - Expected: a "Build Repair" row appears, terminal status matches the
    repair outcome (SUCCEEDED on acceptance, FAILED on rejection).
  - Verify: the repair summary (from `response.explanation` or
    `response.rejection_reasons`) is logged as one line via `append_log`.
- [ ] Every row shows `data-testid="wizard-run-row"` and the badge carries
      `data-status="<lowercase-status>"` (`queued`, `waiting-for-worker`,
      `running`, `verifying`, `waiting-for-user`, `succeeded`, `failed`,
      `canceled` — exact values from `models.WizardRunStatus`).

## 2. Live log tail (SSE)

- [ ] While a run is non-terminal (or just after a terminal one, logs persist
      regardless), click **Logs** on its row.
  - Expected: the log panel opens, shows "Connecting…" briefly, then lines
    stream in as `[level] message` (e.g. `[info] ...`), auto-scrolling to the
    bottom.
  - Verify (Network tab): a `GET .../wizard-queue/{id}/logs/stream?access_token=...`
    request with `Content-Type: text/event-stream` stays open (pending), and
    `data: {"seq":N,"level":"...","message":"..."}` frames arrive as the
    underlying build logs progress.
- [ ] Click **Hide logs** (button toggles to that label while open).
  - Expected: the EventSource closes (Network tab shows the stream request
    finish/cancel) and the panel collapses. Re-opening starts a fresh stream
    from the persisted backlog (no duplicate/missing lines — replay is by
    persisted `seq`).

## 3. SSE → poll fallback

- [ ] Open DevTools → Network → find the open `.../logs/stream` request →
      right-click → **Block request URL** (Chrome) or use `chrome://net-export`/
      request-blocking equivalent to force it to fail, OR set the browser
      offline (Network conditions → Offline) for ~5s then back online, OR (a
      DevTools-console alternative) throttle/kill the connection so
      `EventSource.onerror` fires.
  - Expected: `es.onerror` in `wizard-queue.js` fires, the stream is torn down
    (`_teardownStream`), and `_startLogPoll` kicks in — a `GET
    .../wizard-queue/{id}/logs?after_seq=<lastSeq>` request fires immediately,
    then every **2000ms** (`LOG_POLL_MS`).
  - Verify (Network tab): after the SSE block, `.../logs?after_seq=N` requests
    appear at a steady ~2s cadence, and the log panel keeps appending new
    lines (log content is unbroken across the fallback — no gap in `seq`
    beyond what was already delivered).
- [ ] Un-block the URL / restore network and close+reopen the Logs panel.
  - Expected: SSE resumes as the primary path on the next open (the fallback
    is per-open-session, not sticky).

## 4. Cancel + cascade

- [ ] Start a long-running build (Node Wizard on a slow node, or an
      Agent/Project run) so its WizardRun is non-terminal, then click
      **Cancel** on its row (only rendered when `can_cancel` is true, i.e. the
      run is not yet terminal and the caller passes `can_access`).
  - Expected: the row's status flips to `canceled` (terminal, red/muted
    badge) within one refresh cycle (poller base 5s, or click **Refresh**).
    Cancel/Retry/Dismiss buttons update accordingly (Cancel disappears,
    Retry + Dismiss appear).
  - Verify (API): `POST /wizard-queue/{id}/cancel` → 200 with
    `status: "canceled"`. A 409 is returned if the run was already terminal
    (race: it finished between page load and the click).
  - Verify (PSQL): `select status from wizard_runs where id='<id>';` → `canceled`.
- [ ] **Cascade**: cancel a run that has non-terminal **child** runs
      (`parent_run_id` set — today this applies to an AGENT_PROJECT run whose
      node builds are linked as children via `emit_build_run(parent_run_id=...)`).
  - Expected: canceling the parent also HARD-kills and marks CANCELED every
    non-terminal child (`services/wizard_queue.py::cancel_run` queries
    `WizardRun.parent_run_id == run.id AND status NOT IN (terminal) AND
    deleted_at IS NULL`, then recurses one level). Each child gets a log line
    "Canceled via cascade from parent run `<parent-id>`".
  - Verify (PSQL): `select id, status, parent_run_id from wizard_runs where parent_run_id='<parent-id>';`
    → all `canceled`.
  - Verify (log line): open the child's Logs panel (or `GET
    /wizard-queue/{child_id}/logs`) and confirm the cascade line is present.
  - Note: cascade is exactly one level (parent → direct children); it does
    NOT recurse to grandchildren, because only AGENT_PROJECT emits
    `parent_run_id` today — no run type currently nests two levels deep.
- [ ] Cancel a run whose native kill hook fails (hard to force manually;
      alternatively read `services/wizard_queue.py::_hard_kill_native` — it
      never raises past the cancel call). Confirm from code: even if the
      underlying `WorkflowRun`/`SupportChatSession` kill throws, the
      WizardRun still transitions to CANCELED (best-effort kill, but the row
      always reflects the user's cancel intent).

## 5. Retry link

- [ ] Find (or produce, by pointing a Stack Wizard prompt at something that
      fails, or canceling a run per §4) a row with status `failed` or
      `canceled`, and click **Retry** (only rendered when `can_retry` is true
      — `WIZARD_RETRYABLE_STATUSES = {failed, canceled}`).
  - Expected: a **new** row appears (same wizard type, target name, owner,
    tenant/platform scope as the original — `retry_run` copies
    `origin`/`is_platform_level` from the original so a tenant run's retry
    stays tenant-scoped no matter who clicks it) at status `queued`, showing
    `retry of <original-run-id>` under the target name (`run.retry_of_id`
    rendered via `_renderRow`'s `retryBackLink`).
  - Expected (dispatch):
    - **STACK**: re-runs the wizard LLM pre-fill with the original prompt
      (`extra.prompt`, stored at creation); the new run gets a log line
      "retry queued — native re-dispatch pending operator/backfill wiring" is
      replaced by actual dispatch (see `_retry_stack`).
    - **CONTAINER_ADAPTER**: re-enqueues a fresh `ApiTaskQueue` task via
      `_enqueue_container_task`, using the stored `container_env`; the new run
      gets a log line "retry re-enqueued as task N" and a new `target_ref_id`.
    - **NODE** / **BUILD_REPAIR**: currently only create the queued row +
      an audit log line "retry queued — native re-dispatch pending
      operator/backfill wiring" (no fresh native dispatch is wired yet —
      this is documented, intentional scope in `wizard_queue_emitters.py`
      hook registration, not a bug).
  - Verify (PSQL): `select id, retry_of_id, status, target_ref_type,
    target_ref_id from wizard_runs where retry_of_id='<original-id>';`
  - Verify: the **original** row keeps its terminal status unchanged (retry
    never mutates the original — append-only history).
- [ ] Retry a run that is still non-terminal.
  - Expected: `POST /wizard-queue/{id}/retry` → 409
    (`only failed/canceled runs can be retried`); the Retry button is not
    rendered for such a row in the first place (`can_retry` is false), so
    this is really an API-level check — confirm via curl.

## 6. No email is sent

- [ ] Let a wizard run reach a terminal state (succeeded or failed).
  - Expected: an in-app **Notification** row is created for the owner (+ the
    relevant tenant admins, or all platform admins for a platform-level run)
    — `services/wizard_queue.py::_create_terminal_notifications` — visible as
    a bell/toast notification, and a realtime publish
    (`routers.notifications.publish_notification_event`). No outbound email
    is ever sent for a WizardRun transition:
    - `grep "send_email\|sendgrid\|smtp\|ses\.\|email.*wizard"` over
      `wizard_queue.py`, `wizard_queue_emitters.py`, and `routers/wizard_queue.py`
      returns zero hits
    - The module docstrings explicitly state "emit bell + toast
      notifications on completion/failure — NO email (D18)".
  - Verify (UI): the Dashboard/WB notification bell shows a new item
    `"<type> build '<target>' completed"` or `"... failed"`, with
    `link_url = #wizard-queue/<id>`.
  - Verify (negative): `docker logs my_fastapi_app --since 5m | grep -i "smtp\|sendgrid\|ses\.\|email.*wizard"`
    → no hits tied to this run's completion. (If your deployment has SMTP
    configured for other features, e.g. password reset, this step is only
    checking for a wizard-run-triggered send, not the absence of SMTP
    config entirely.)

## 7. Three-role visibility matrix

Server-side scoping (`services/wizard_queue.py::scope_query` /
`can_access`) is mirrored client-side by
`isOwnScope()` in `wizard-queue.js` (belt-and-suspenders). A run is
**platform-level** (`is_platform_level=True`) only when created by a SYSTEM
process or a platform admin; otherwise it is tenant-scoped.

| Role | Sees | Does NOT see |
|---|---|---|
| Platform admin | Runs where `is_platform_level = true` only | Any tenant-scoped run, even from their own actions taken while impersonating/acting for a tenant |
| Tenant admin (`parent_user_id IS NULL`) | Tenant-scoped runs (`is_platform_level = false`) for their own `tenant_id`, regardless of which user in the tenant created them | Platform-level runs; other tenants' runs |
| Sub-user (`parent_user_id IS NOT NULL`) | Only their own runs (`owner_user_id = self`), tenant-scoped | Their tenant admin's or teammates' runs; platform-level runs |

- [ ] As `admin` (tenant admin, tenant A), create a Stack Wizard run.
  - Expected: it appears in `admin`'s Activity tab.
  - Verify (API): `GET /wizard-queue` as `admin` includes it;
    `is_platform_level=false`, `tenant_id=<tenant A id>`.
- [ ] As a **sub-user** of tenant A who did NOT create that run, open Activity.
  - Expected: the tenant-admin-created run from the prior step is **absent**
    (`scope_query` filters sub-users to `owner_user_id == self`).
- [ ] As that same sub-user, create their own Node Wizard build (via Build
      Queue panel — note NODE runs appear there, not in the Activity tab).
  - Expected: the NODE run appears in the Build Queue panel, not the Activity
    tab. Create an AGENT_PROJECT run instead (via AI Assistant) to test
    sub-user visibility in Activity.
  - Expected: the AGENT_PROJECT run appears in the sub-user's own Activity list.
    It also appears for `admin` (tenant admin sees ALL of tenant A's runs).
  - Expected: it does NOT appear for a user in a different tenant B.
- [ ] As a user of **tenant B**, open Activity.
  - Expected: neither tenant A run is visible (`tenant_id` mismatch filters
    them out even though tenant B's user is also a tenant admin/sub-user).
- [ ] As the **platform admin**, open Activity.
  - Expected: **none** of the tenant A/B runs from the steps above are
    visible — only rows with `is_platform_level=true` (created by a SYSTEM
    process, e.g. a scheduled maintenance rebuild, or by a platform admin
    action). This is the "platform-level-only" rule reconciled into
    `the internal design notes` (GE.WQR.12) —
    platform admin is explicitly NOT a tenant-run superuser view here.
  - Verify (PSQL): `select count(*) from wizard_runs where is_platform_level=false and tenant_id in (<A>,<B>);`
    is nonzero, while the platform admin's `GET /wizard-queue` response
    shows none of those ids.
- [ ] Cross-check action authorization matches visibility: as tenant B's
      user, `POST /wizard-queue/{tenant-A-run-id}/cancel` →
      **404** (not 403 — `_require_access` deliberately 404s to avoid leaking
      existence across tenants, same convention as `node_runs`).

## 8. Redaction check

- [ ] Trigger a BUILD_REPAIR run and plant a fake secret `sk-live-TESTSECRET`
      in the repair LLM response (`api_key=sk-live-TESTSECRET` in the
      explanation or rejection_reasons).
  - Expected: the secret is absent from both `GET /wizard-queue/{id}/logs` and
    `GET /wizard-queue/{id}/logs/export?format=json|csv`.
  - Verify (from test): `test_wizard_queue_repair_emitter_realdb.py` plants
    `sk-live-TESTSECRET` in the LLM's `explanation`, confirms it's absent from
    logs/export, and asserts `<API_KEY>` appears in place.
  - Verify (PSQL): `select message from wizard_run_logs where run_id='<id>'
    and message like '%sk-live-%'` → no rows returned.

## 9. Cleanup / rollback notes

- [ ] After testing, click **Clear completed** to soft-delete all terminal
      runs (only those in the caller's scope). This prevents pollution of the
      queue view for other testers.
  - Verify (PSQL): `select count(*) from wizard_runs where deleted_at is not null;`
    increased after clear.
- [ ] Dismiss individual runs via their **Dismiss** button after testing.
  - Verify (PSQL): `deleted_at` and `deleted_by_user_id` are set.
- [ ] To roll back the Wizard Queue feature:
  1. **Dashboard**: `git revert` (or hand-edit back) the three Dashboard files:
     `CORE/Dashboard/js/wizard-queue.js` (delete/restore),
     `CORE/Dashboard/pages/analytics.html` (remove the `#main-tab-activity`
     button and `#main-panel-activity` block),
     `CORE/Dashboard/js/analytics.js` (remove `'activity'` from the allowed
     tabs array at ~L56, the `main-panel-activity` branch at ~L103-146, and
     the tab-reveal line at ~L940).
  2. Rebuild + recreate the Dashboard container by hand — **do not** rely on
     `docker compose up` for this stack (compose is broken here per the
     deploy-frontend-containers note); build the image directly and recreate
     `mod_dashboard` manually.
  3. Hard-refresh the Dashboard and confirm the Activity tab/button is gone
     and no console errors reference `wizard-queue.js`.
  4. API-side rollback: `docker restart my_fastapi_app` after reverting
     `routers/wizard_queue.py` / `services/wizard_queue*.py` — no migration to
     undo (the `wizard_runs`/`wizard_run_logs` tables are additive and
     harmless to leave in place even with the routes removed).

## Edge cases

- [ ] Empty state: a tenant/role with zero in-scope runs sees "No wizard
      runs." in the rows area (`_renderRows`'s empty-state string) and
      Prev/Next both disabled, page info `0-0 of 0`.
- [ ] Pagination: seed >20 in-scope runs, confirm **Next** advances
      `offset` by 20 and **Prev** goes back, with the `X-Y of N` label
      matching `GET /wizard-queue?limit=20&offset=...`'s `total_count`.
- [ ] Clear completed: click **Clear completed**.
  - Expected: `POST /wizard-queue/clear-completed` soft-deletes every
    terminal, in-scope run for the caller (`deleted_at` set, `deleted_by_user_id`
    set — never a hard delete, ISO no-hard-delete). They disappear from the
    list; non-terminal runs are untouched.
  - Verify (PSQL): `select count(*) from wizard_runs where deleted_at is not null;`
    increases; row count unchanged (soft-delete only).
- [ ] Dismiss a single terminal run via its **Dismiss** button — same
      soft-delete, scoped to one row. Attempting to dismiss a non-terminal
      run via the API (`POST .../dismiss` on a `running` run) → 409.
- [ ] Log export: `GET /wizard-queue/{id}/logs/export?format=csv` and
      `?format=json` download the full (already-redacted) log history with
      the correct `Content-Disposition` filename.

## Visible failure modes

- [ ] Kill `my_redis`/Valkey while a log stream is open.
  - Expected: the SSE `pubsub.get_message` calls start erroring; the stream
    ends and the client falls back to polling per §3. New log lines stop
    arriving (nothing publishes) but the panel does not error/crash — it
    just goes quiet until Redis returns.
- [ ] Kill `my_fastapi_app` (or block `/wizard-queue*` in DevTools) while the
      list poller is running.
  - Expected: per the poller-self-heal design (`BackoffPoller` in
    `wizard-queue.js`), failures back off from 5s toward 60s and then keep
    probing at 60s forever — the panel does not hard-stop and silently die;
    it recovers automatically once the API answers again, no manual refresh
    needed.
- [ ] Load Activity as a signed-in user whose `/auth/me` (`me.get()`) call is
      slow/fails.
  - Expected: `_currentUser` stays `null` until it resolves; the very first
    paint trusts the server's own scoping (no client-side over-filtering to
    an empty list because `_currentUser` isn't loaded yet) — confirm no rows
    incorrectly vanish on first load for a legitimate user.
