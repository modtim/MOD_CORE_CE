---
sidebar_position: 62
slug: /runbooks/container-log-rotation
title: Container log rotation (size caps)
description: "Runbook: how MOD bounds the on-disk size of each container's stdout log, which settings control it, how to change them safely, how to verify the cap is actually applied on a running container, what to roll back, and how container stdout differs from the database system-log archive."
---

# Container log rotation (size caps)

Last reviewed: 2026-10-09

Docker's default logging driver is `json-file` with **no size cap**: a container
that prints a lot can grow its log until the host disk is full. On Community
Edition MOD stamps a size cap on the stdout log of every service in a **managed
stack**, at deploy. Every platform service in the CE compose also ships its own
`logging:` block (the `x-logging: &default-logging` anchor: `json-file`,
`max-size: 50m`, `max-file: 5`), so its stdout is capped at **5 files of 50 MB
per container** with no host configuration at all.

On a fresh install the anchor is in the shipped compose file, so `docker compose up`
**creates every container with the anchor's `logging:` block** from the start.
On an upgrade, re-running `install.sh` (or any `docker compose up -d`) **recreates only
the containers whose configuration changed**; the new `logging:` block is part of that
configuration, so those containers pick it up. A plain `docker compose restart` does not
apply new options, and containers whose configuration did not change keep their old
options until they are next recreated.

This runbook is about that size cap. It is **not** about how long application
log rows are kept — that is a separate mechanism with separate knobs, and the
two are easy to confuse. See [Container logs vs the system-log archive](#container-logs-vs-the-system-log-archive)
below, and Backup and Restore for the archive.

## When to use

- A host is running out of disk and you suspect container logs.
- You are prepping an install for a compliance profile and want the log-size
  policy written down and checkable.
- You want to raise or lower the cap for a specific stack, or pin a different
  driver for one service.

Do **not** use this runbook to recover a lost log. Rotated container logs age
out in write order; once the oldest rotated file is dropped at the cap, it is
gone. Nothing here can bring it back.

## What is live today

| Knob | Where it applies | Default |
|---|---|---|
| Docker daemon default logging driver and options (`log-driver` / `log-opts` in `daemon.json`) | platform services — each one carries a `logging:` block (the `x-logging: &default-logging` anchor), so `daemon.json` does not decide their cap | **5 files of 50 MB per container** (`max-size: 50m`, `max-file: 5`) |
| `logging.container_max_size` (platform setting) | every managed stack service, stamped at deploy | `50m` |
| `logging.container_max_files` (platform setting) | every managed stack service, stamped at deploy | `5` |

Worst-case footprint per container is `max-size × max-file` — 250 MB at the
defaults. Docker rolls the file at the cap and keeps at most `max-file` files.

Two things that are true today and worth stating plainly:

- The cap is a **size** policy only. It is not a retention window, and it does
  not archive anything.
- A stack service that already declares its own `logging:` block is never
  overwritten by the stamp, so you can pin a different driver or size per
  service by editing that service's compose.

## Pre-checks (confirm each before starting)

- [ ] **You are changing a policy, not chasing a symptom.** If the disk is full
      now, free it first; a new cap does not shrink a file that already grew.
      The cap only takes effect on the next deploy/recreate of the container.
- [ ] **A drain.** No in-flight runs, tasks or stack deploys, using the same
      drain check as [Restart the API without losing runs](/runbooks/api-restart-and-drain).
      Applying a new cap means recreating containers, and recreating a container
      mid-run loses that run.
- [ ] **The current values recorded.** Write down the setting values, and for
      the platform services the host's current `daemon.json` `log-driver` and
      `log-opts` keys (or their absence), so you have something to roll back to.
- [ ] **A token with platform-settings write rights.** The change is an admin
      API call, and it is audited like any other platform-settings change.
- [ ] **A stopwatch and a log.** One recorded result per verify check below.

## Steps

1. **Read the current policy.**

   ```bash
   curl -s "$MOD_API_BASE/v1/platform/settings/logging.container_max_size" \
     -H "Authorization: Bearer <ADMIN_TOKEN>" | jq .
   curl -s "$MOD_API_BASE/v1/platform/settings/logging.container_max_files" \
     -H "Authorization: Bearer <ADMIN_TOKEN>" | jq .
   ```

2. **Set the values you want.** Sizes are strings such as `50m` or `100m`; the
   file count is an integer. Platform-settings writes are gated by
   `auth.privileged_reauth_max_age_s` (the reauth gate in `platform_settings.py`):
   it is `0` = off by default, so a token of any age works. If you have turned it
   on and a call returns 401 with `"error": "reauth_required"`, sign in again and
   retry the same request.

   ```bash
   curl -s -X PATCH "$MOD_API_BASE/v1/platform/settings/logging.container_max_size" \
     -H "Authorization: Bearer <ADMIN_TOKEN>" \
     -H "Content-Type: application/json" -d '{"value": "100m"}'
   curl -s -X PATCH "$MOD_API_BASE/v1/platform/settings/logging.container_max_files" \
     -H "Authorization: Bearer <ADMIN_TOKEN>" \
     -H "Content-Type: application/json" -d '{"value": "7"}'
   ```

   The setting is stored; it does not reach any running container yet. The cap
   is stamped onto a stack service at deploy time, so it applies on the next
   deploy or redeploy of that stack.

3. **Platform services — host configuration.** The CE compose ships **no**
   `logging:` block and **no** log-size environment variable for the platform
   services, so there is nothing in the compose file to edit and no env var to
   point at new values. Their stdout cap is the Docker daemon's own default, and
   you set it on the host. In `/etc/docker/daemon.json` (host config, not part
   of the install):

   ```json
   {
     "log-driver": "json-file",
     "log-opts": {"max-size": "50m", "max-file": "5"}
   }
   ```

   `docker info` prints the effective default driver and its options, so you can
   confirm the daemon picked the keys up. Applying it needs `systemctl restart docker`,
   which restarts dockerd — drain first and land it in the same window you would
   use for any container recreate; see
   [Restart the API without losing runs](/runbooks/api-restart-and-drain). Do not
   bring the stack up mid-run just to pick up a logging change.
   The cap only takes effect on containers created after it, so existing services
   stay uncapped until you recreate them (Step 5).

4. **Per-service pin (optional).** Add a `logging:` block to that one service in
   its compose. The stamp skips any service that already declares one, so your
   pin survives every deploy and reconcile.

5. **Recreate the affected containers** after the drain, in the order your
   install normally brings them up.

## Verify

Run these in order; the change is not done until every line passes.

```bash
# 1. The settings hold the values you set.
curl -s "$MOD_API_BASE/v1/platform/settings/logging.container_max_size" \
  -H "Authorization: Bearer <ADMIN_TOKEN>" | jq .
curl -s "$MOD_API_BASE/v1/platform/settings/logging.container_max_files" \
  -H "Authorization: Bearer <ADMIN_TOKEN>" | jq .

# 2. A stack service actually got the cap stamped on it.
docker inspect --format '{{json .HostConfig.LogConfig}}' <CONTAINER> | jq .

# 3. Rotated files exist and the count never exceeds max-file.
ls -1 /var/lib/docker/containers/<CONTAINER_ID>/ | grep -c 'json.log'
df -h /var/lib/docker
```

| Check | Expected |
|---|---|
| `LogConfig.Type` | `json-file` |
| `LogConfig.Config.max-size` / `max-file` | the values you set |
| Rotated-file count | at most `max-file`, never more |
| A stack redeploy after the change | its rendered compose carries a `logging:` block |
| Audit continuity | the settings change appears in the audit trail |

The second check is the one that matters: a setting that reads correctly is not
proof a running container is capped, because the cap is applied when the
container is created. Check a container created **after** the change.

## Rollback

- **Settings.** PATCH them back to the values recorded in the pre-checks. Only
  containers created after that are affected, so roll back before the next
  deploy rather than after.
- **daemon.json / per-service pin.** Restore the file you edited (the host's
  `/etc/docker/daemon.json`, or the `logging:` block you wrote in a stack
  service's compose) and let the change land on the next planned restart
  window. Do not recreate a container mid-run to undo a logging change.
- **Disk pressure.** Freeing disk is not a rollback of the cap; if you deleted
  rotated log files to recover space, that data is gone and cannot be restored.
  Note in the incident record what was removed and when.
- **Nothing here rolls back an audit record.** The audit trail is append-only; a
  logging change is recorded in it and stays recorded.

## Container logs vs the system-log archive

These are two different stores with two different policies. Do not conflate them.

| | Container stdout logs (this runbook) | System-log archive |
|---|---|---|
| What it is | `json-file` driver output on the host | application log rows in the database |
| Where | `/var/lib/docker/containers/<id>/<id>-json.log` plus rotations | the database, exported nightly to the archive bucket |
| Bounded by | **size** — `max-size` × `max-file` per container | **time** — `logs.searchable_days` (90) / `logs.archive_days` (365) |
| Rotates | yes — oldest file dropped at the cap | no hard delete — old rows are soft-marked, archived nightly with a SHA-256 manifest |
| Retention | a bounded window of files, not a calendar window | 90 days searchable, 1 year archived |

Changing `logging.container_max_*` does not change how long database log rows
are kept. Changing `logs.searchable_days` does not change how big a container
log file may grow.

Container logs rotate, and the oldest file is discarded at the cap. That is
intentional: container stdout is operational signal, not a compliance record.
Audit records are the opposite — the audit trail is append-only and is never
deleted by log rotation, because no audit record is written to container stdout.

Do not set `max-file: 1` or a tiny `max-size` on a service whose stdout you
might want for forensics. That only shortens your operational window; it does
not touch the audit trail, and it does not help compliance.

## Planned, not live yet

- **A rotation script for container logs.** There is none. Docker's own size-cap
  rotation is the only mechanism today; nothing on the host runs a scheduled
  log-rotation job for container stdout, and nothing compresses or ships rotated
  files on a timer.
- **Per-container caps.** The settings are platform-wide. A
  per-stack override is not built; the only per-service control
  today is a `logging:` block you write in that service's compose.
- **Alerting on log caps.** The platform does not alert you when a container
  log reaches its size cap; Docker rotates silently and drops the oldest file.
  Docker **disk** pressure is handled differently: on a host where the API can
  reach the Docker socket, a scheduler job runs the Docker disk monitor every
  15 minutes. Above 85% used it prunes build cache (`docker builder prune -af`);
  above 95% it also prunes unused images (`docker image prune -af`); if usage is
  still above 90% after the prune it logs a `PRESSURE WARNING`. Thresholds are
  set by `DOCKER_DISK_AUTO_CLEAN_THRESHOLD`, `DOCKER_DISK_DEEP_CLEAN_THRESHOLD`
  and `DOCKER_DISK_ALERT_THRESHOLD`; `DOCKER_DISK_MONITOR_DISABLED=true` turns
  it off. Where the API has no socket view the monitor does nothing, so watching
  the disk beyond that is yours. In the shipped Community Edition compose the API
  container has no view of the host's Docker data root, so this monitor does not
  run there; watching free disk and removing old images is yours.
- **Log forwarding from rotated files.** Rotated files are read by whatever you
  point at them; the platform does not forward them.

## Related

- Backup and Restore — what the backup set
  covers, and the system-log archive retention.
- [Restart the API without losing runs](/runbooks/api-restart-and-drain) — the
  drain check used before applying a change.
- [First minutes of an incident on your install](/runbooks/incident-first-steps) —
  how to read a container's log without capturing credentials in it.
