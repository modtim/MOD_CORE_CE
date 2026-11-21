---
sidebar_position: 53
slug: /runbooks/worker-enrol-and-retire
title: Enrol a worker, or retire one without losing its work
description: "Runbook: bring a compute box into a self-hosted MOD install through a single-use enrollment token, and take one out by draining it first so no in-flight task is abandoned."
---

# Enrol a worker, or retire one without losing its work

Last reviewed: 2026-10-07

Bring a new box into your install through a single-use enrollment token, and
take a box out of the fleet by draining it first so no in-flight task is
abandoned.

## When to use

- A box is being installed for the first time, or re-installed after a
  decommission.
- A box is being pulled: hardware swap, lease end, permanent removal.
- Do NOT use this to pause a box for a day — pausing alone is enough and is
  reversible. Do not use it while a reboot-window runbook is mid-flight.

## Pre-checks

Set the base once and get `$TOKEN` from the Getting Started token step
(`$BASE` has no `/v1`; the paths below carry it):

```
BASE=http://localhost:8000
AUTH="Authorization: Bearer $TOKEN"
```

- [ ] `curl -sk -H "$AUTH" $BASE/v1/workers` — expect the fleet list; the new
      box is absent, the retiring box is present and approved.
- [ ] `curl -sk -H "$AUTH" $BASE/v1/workers/license-summary` — expect the
      the count of approved workers; Community Edition has no worker cap, so
      the limit reads unlimited. The last worker on the install cannot be
      removed (see Steps).
- [ ] Retiring only: `curl -sk -H "$AUTH" $BASE/v1/workers/{worker_name}/queue`
      — expect `in_flight` and `assigned` lists. Both must be empty before
      the box is removed.

## Steps — enrol

1. Mint the enrollment token (platform admin).
   These commands use $BASE and $AUTH from Pre-checks; run that block first in the same shell.
   ```
   curl -sk -X POST -H "$AUTH" $BASE/v1/workers/enrollment-tokens \
     -H 'Content-Type: application/json' \
     -d '{"label":"<box-label>","max_uses":1,"auto_approve":false,"expires_in_hours":24,"home_anchor":"local"}'
   ```
   Expect one JSON body carrying `token` exactly once — it is stored hashed,
   so a lost token means a new one. The manual flow is
   `max_uses=1, auto_approve=false`; an auto-enrolment token is
   `max_uses=N, auto_approve=true, auto_license=true` with a name pattern.
   `home_anchor` is `local` (the install's own network, survives a WAN cut)
   or `remote` (a fixed external anchor).
2. The response also carries the box's network-join credentials for the
   install's private network plane — single-use, with a TTL from the
   platform network settings (default 60 minutes,
   range 5-1440). Do not mint such a key by hand, and do not print the
   response: capture it to a file and shred it after use.
3. Start enrollment from the box: `POST /v1/workers/enroll/start` with
   `worker_name` plus the token. Expect an approval code and a bootstrap
   secret. Enrollment attempts are rate-limited per IP; a failed attempt
   can emit an admin alert, rate-limited per worker name.
4. Prove the pairing out-of-band: `POST /v1/workers/enroll/confirm` with
   `approval_code` plus `bootstrap_token`. This does not approve the worker;
   it proves both sides hold their secret.
5. Approve (unless the token had `auto_approve`): from the Dashboard, the
   Workers page — the PENDING row shows an **Approve** button. Or call the
   API: `POST /v1/workers/enroll/approve/{worker_name}`.
6. Poll `GET /v1/workers/enroll/status/{worker_name}` — expect
   `status: approved` and an approved worker once the box heartbeats.

## Steps — retire

Order matters. Drain, then deactivate, then remove.

1. **Stop new work on the box.**
   ```
   curl -sk -X POST -H "$AUTH" $BASE/v1/workers/{worker_name}/pause
   ```
   Expect `paused=true`; the next heartbeat response carries `drain=true` and
   the worker stops polling for tasks.
2. **Wait for what it holds.** Poll
   `GET /v1/workers/{worker_name}/queue` until `in_flight` and `assigned`
   are both empty. Do not proceed on a "looks idle" guess.
3. **Take the stacks off it.** A worker hosting persistent stacks cannot be
   removed; tear down or migrate each stack first
   (see [Deploy and recover a persistent stack](/runbooks/stack-deploy-and-recover)).
4. **Deactivate the worker.**
   ```
   curl -sk -X POST -H "$AUTH" $BASE/v1/workers/revoke-license
   ```
   Expect the worker to show as not approved and paused, with a `revoked_at`
   stamp, and its per-worker Vault AppRole is dropped when the AppRole mode is
   not off and the AppRole module is installed (on CE nothing is dropped); a
   failure is logged and never blocks the deactivation. This is the step that
   takes the box out of the task pool.
5. **Remove the row only if the box is being decommissioned** (platform
   admin, and the worker must already be offline or revoked):
   `DELETE /v1/workers/{worker_name}`. Expect an audit event
   `WORKER_DELETED`. Refusals you may see: the last worker on the install,
   an active (approved) worker, or a worker still hosting stacks. Note: this
   removal is a documented exception to the no-hard-delete rule — the worker
   name columns are plain unique, so a soft-deleted row would keep owning the
   name and block re-enrolment of the same box. The decommission itself is
   retained in the audit trail, not in a row. If you only want the box out
   of rotation, stop at step 4.
6. **Revoke the enrollment token** so a used token cannot admit another
   device: `DELETE /v1/workers/enrollment-tokens/{token_id}`.

## Verify

- Enrol: `GET /v1/workers` shows the box, approved, with a hardware report;
  `GET /v1/workers/{worker_name}/queue` answers.
- Retire: `GET /v1/workers` no longer lists the box as approved;
  `GET /v1/workers/license-summary` shows the count down by one; no queued
  task points at it.
- Re-enrolment of the same name works after a decommission.

If verification fails, go to Rollback immediately; do not improvise.

## Rollback

- Paused by mistake: `POST /v1/workers/{worker_name}/resume`.
- Deactivated too early: `POST /v1/workers/enrollments/{worker_name}/reinstate`
  — a revoked enrollment row blocks re-enrolment until Reinstate is clicked.
- After step 5 there is no undo: the row is gone. Re-enrol from step 1 with a
  fresh token. Do not try to re-add the row by hand.
- Never hard-delete a worker while it holds tasks — step 2 is what protects
  that; a task orphaned against a missing worker has to be re-driven.

## Planned

- Revoking a retired worker's node on the install's private network plane:
  planned. Until that step exists, the node stays on the plane and the
  network's access policy governs reachability.
