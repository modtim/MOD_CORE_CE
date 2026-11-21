---
sidebar_position: 56
slug: /runbooks/incident-first-steps
title: First minutes of an incident on your install
description: "Runbook: what an operator of a self-hosted MOD install does in the first minutes of a security incident on their own install — stop the bleed, preserve the evidence, find the audit and health views, drain before restarting the API, and when to bring Mod Tech Labs Inc. support in."
---

# First minutes of an incident on your install

Last reviewed: 2026-11-11

You run your own MOD install and you suspect unauthorized access, a leak, or a
misbehaving account or tenant. The first minutes have three jobs, in order:
**contain the affected party, preserve the record, then keep the install
running without losing evidence.** Do them in that order — containing before
exporting destroys what you are trying to prove.

This is for a security incident on *your* install. For a benign outage use
[Restart the API without losing runs](/runbooks/api-restart-and-drain) instead.

## When to use

- You see a credential in the wild, an account doing something it should not,
  a tenant reaching data it was never granted, or a report from a customer.
- Not for a broken workflow, a failed deploy, or a worker that went quiet —
  those are not incidents and should not open an incident record.

## Pre-checks

Set `MOD_API_BASE` to your install's API URL (the one you open the Dashboard on;
on a local CE install that is `http://localhost:8000`). The curl examples on this
page use `$BASE` for that same URL; define it once and replace it with your API
address for a LAN install. `$BASE` has no `/v1`; the paths carry it:

```
BASE=http://localhost:8000
```

- [ ] The API is answering: `curl -sk $BASE/v1/health` — expect a
      `status` of `ok`. Fix an outage first; the audit rows this runbook reads
      and the routes it calls all go through the API.
- [ ] You are signed in as a **platform admin** on the Dashboard. The audit-log
      and platform-settings routes this runbook uses require it; an ordinary
      tenant admin gets a 403.

## 1. What CE gives you as the record

**The incident tracker is not part of Community Edition.** The
`/v1/platform/security-incidents` routes (create, confirm, follow-ups, notify,
close) are not mounted in CE, and neither is the compliance incident-export
surface: those are not part of this edition. Do not look for an `incident_id`, a
severity field or deadline math on a CE install — there is nothing to open.

What CE does keep is the record itself, and it is the stronger half:

- **The audit trail.** Every meaningful action already wrote a row — grants,
  revocations, key creates, refusals. It is append-only, nothing edits or
  removes it, and it is your timeline. Read it in the Dashboard **Audit Logs**
  page (`GET /v1/audit-logs/permissions/{log_id}` for a single row).
- **Container logs for the window** (see Step 3 below): capture them through a
  redaction filter before you touch anything.
- **The platform settings `incident.breach_notify_hours` and
  `incident.government_headsup_hours` still exist** as settings, but with no
  incident tracker mounted in CE nothing on the install drives them; the
  deadline math is not part of this edition.

Write down the window (`ISO_START`/`ISO_END`), the affected party, and what you
suspect in a plain note next to the evidence — on CE that note is your record,
and the audit rows are the proof.

## 2. Stop the bleed

Contain in this order, and only these ways:

1. **Revoke the party's keys** — `DELETE $BASE/v1/api-keys/{API_KEY_ID}`.
   Revoking invalidates already-issued tokens, so a stolen token stops working
   immediately.
2. **Take the tenant's own credentials** — rotate the tenant's Keycloak client
   secrets and revoke its API keys (the step above). **Tenant
   suspend/reactivate is not part of CE**: the `/v1/platform/tenants/{TENANT_ID}/suspend`
   and `/reactivate` routes are not mounted in the CE build, so there is no
   lifecycle move to expect on a CE install. Revoke the keys and stop the
   affected stack (`POST /v1/stacks/{stack_id}/stop`) instead; both are soft and
   audited.

**Never delete anything during an incident.** Suspend, revoke and archive are
soft (`revoked_at`, `lifecycle_state`); the audit trail is append-only. Do not
purge logs, keys, or tenant records, and do not `docker rm` or reboot the install
to "clean it up" — that erases the evidence you still need.

## 3. Preserve evidence — before you contain anything else

Two things hold the record, and both must be captured while the incident is
fresh.

**The audit trail.** Every meaningful action on your install already wrote a
row: grants, revocations, key creates, exports, refusals. Nothing in it is
edited or removed, so the trail is your timeline. Read it in the Dashboard
**Audit Logs** page. There is no incident-export route in CE
(`/v1/platform/compliance/incident-exports` is not mounted — compliance export
is not part of this edition), so you snapshot the window yourself: page the audit rows for
`{ISO_START}`..`{ISO_END}` out of the Dashboard **Audit Logs** view (or
`GET /v1/audit-logs/permissions/{log_id}` per row) and keep them with the logs.
Hash the files you keep — `sha256sum` on your own copy is the proof that the
bundle you are holding is the one you took that day.

**Container logs for the window.** Capture them through a redaction filter,
always, so a credential in a request line never lands in your evidence:

```
docker logs --since {ISO_START} {CONTAINER} 2>&1 | grep -vE "Bearer|authorization|access_token"
```

Keep the filter. An evidence bundle with a live token in it is a second
incident.

## 4. Where the views are

| What you want | Where |
| --- | --- |
| What happened, in order | Dashboard **Audit Logs** |
| The incident, its severity, deadlines, follow-ups | not in CE — the incident routes are not mounted, so keep your own note (Step 1) |
| Is the install healthy right now | `GET $BASE/v1/health`, and the Dashboard **status** page for the install-wide view |
| The affected party's grants and keys | Dashboard **The Guard - Permissions**, and `GET $BASE/v1/api-keys` for the key list (`curl -sk -H "Authorization: Bearer $TOKEN" $BASE/v1/api-keys/`) |

If a view you expect is not there, say so in your note rather than guessing —
your note plus the audit rows are the record.

## 5. Drain before you restart the API

An incident and an API restart often overlap: you want a fix shipped, and a
restart mid-incident can look like tampering and can lose in-flight work.
Follow [Restart the API without losing runs](/runbooks/api-restart-and-drain):
pause dispatch from the platform-settings registry, wait until running workflow
runs and queued platform tasks are both zero, then restart. Do not restart on a
"looks idle" guess, and do not let the downtime pass the worker heartbeat lease
(default 120 seconds) — past it, workers stop the persistent stacks they host
and every stack pays a stop-and-wake cycle.

Restarting the API does not clear anything. The audit rows survive it; your
note and your hashed evidence files survive it too, because they live outside
the install; the containers' logs do not survive a `docker rm`, which is why
Step 3 says capture them first.

## Verify

- `curl -sk $BASE/v1/health` — expect `ok`.
- `scripts/ce-doctor.sh` — expect every service `OK` (no WARN/FAIL): the install
  is healthy and nothing was OOM-killed while you worked.
- The revoked key no longer works: `GET $BASE/v1/api-keys` does not list
  it, and a token minted from it is refused.
- The audit trail carries your action: the newest row is the revocation you just
  made (open it with `GET /v1/audit-logs/permissions/{log_id}`).
- Your evidence files still hash to the values you recorded when you took them.

If verification fails, go to Rollback; do not improvise a second attempt.

## Rollback

- **Wrong note (severity/summary):** append a dated line to your own note
  recording the correction. Never rewrite the earlier line — the audit trail
  stays the record either way.
- **Revoked the wrong key:** you cannot un-revoke it (`revoked_at` is one-way).
  Issue a fresh key for the affected party through the normal path and say in
  your note that the revocation was a mistake; the audit row for it stays.
- **Stopped a stack you should not have:** redeploy it
  (`POST /v1/stacks/{stack_id}/deploy`) — a stop keeps the row, spec and data,
  so the redeploy restores it.
- **There is nothing to "un-suspend" and nothing to notify/close:** tenant
  suspend/reactivate and the incident notify/close routes are not in CE, so
  there is no lifecycle state and no deadline clock to clear.

## When to bring Mod Tech Labs Inc. support in

You own your install and its incident record — support is not your first call.
Contact Mod Tech Labs Inc. support when:

- the incident is about platform behaviour rather than your data: a control you
  expected to block something did not, or an endpoint refused something it
  should have allowed;
- you need the platform's own record (audit rows) verified against what you are
  holding;
- you are about to revoke a key or stop a tenant's stack and want the soft path
  confirmed before you act (CE has no tenant soft-delete — say so and describe
  what you did instead);
- a control you expected to block something did not, and you want to know whether
  that is CE behaviour or your own configuration.

Attach your note, the audit rows, the hashes of your evidence files, and the
redacted logs. There is no incident id and no export manifest on a CE install.
Never attach an unredacted log or a live key.

## Related

[Restart the API without losing runs](/runbooks/api-restart-and-drain) ·
Break-glass ·
Backup and restore
