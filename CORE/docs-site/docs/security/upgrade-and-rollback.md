---
sidebar_position: 28
slug: /security/upgrade-and-rollback
title: Upgrade and Rollback
description: How an on-prem Community Edition install moves to a new release (new CE_IMAGE_TAG, docker compose pull, first-run-secrets, up -d), how the one-shot mod-migrate applies migrations, how workers follow the release, and how rollback re-pins CE_IMAGE_TAG.
---

# Upgrade and Rollback

A Community Edition install upgrades by plain `docker compose`: pull the new
release's images, recreate the services, confirm the result. The CE extract
ships no `update.sh` and no `update-system.sh` — those are dev-tree tools, not
part of a CE install.

Nothing here runs `docker compose down -v`, and nothing deletes volumes. Durable
data survives the upgrade itself.

## Pre-checks

Do these before a real run.

- **Backup taken.** CE has no automatic pre-upgrade dump and no
  `MOD_UPDATE_BACKUP_DIR`. Take a `pg_dump` of the app DB yourself first — see
  [Back up an install and restore it onto a new box](/runbooks/ce-backup-and-restore).
- **Drain in-flight runs before the API restart.** Pause dispatch and wait for
  running workflow runs and worker tasks to finish before recreating anything.
  See [Restart the API without losing runs](/runbooks/api-restart-and-drain)
  for the counters to poll. Never hand-restart the API while runs are live.
- **Disk headroom.** The new image set is pulled while the old images are still
  running, so keep room for both until you delete the old layers.
- **Admin token only if you undrain through the API.** `POST /v1/system/resume`
  clears the dispatch pause and needs a platform-admin bearer token. CE has no
  `MOD_UPDATE_TOKEN` env var and no `--dry-run` preview.
- **PostgreSQL major-version guard.** After `docker compose pull` and before
  `docker compose up -d`, run `scripts/pg-version-guard.sh -f docker-compose.yml`
  from the directory that holds `docker-compose.yml`. It is read-only. It exits 1
  (refused) when your database volume was written by a different PostgreSQL major
  than the image you are about to start: the new server cannot open that data, and
  `up -d` would leave the database in a restart loop. Any non-zero exit means stop
  and fix the cause before you start anything. Running `scripts/install.sh` instead
  runs this guard for you before it runs `docker compose up -d`, and it keeps the secrets in
  your `.env`. For a real major-version change, follow the dump-and-restore
  procedure in `docs/postgres-major-version.md` in your repository checkout (a
  plain-text pointer: this page cannot link to that file).

## Upgrade to a new release

The release's image refs come from two variables in the compose file
(`${CE_IMAGE_REGISTRY:-ghcr.io/modtim}/<name>:${CE_IMAGE_TAG:-latest}`).
Before you upgrade, record the current tag. An install that has no
`CE_IMAGE_TAG=` line runs `latest`.

```bash
grep '^CE_IMAGE_TAG=' .env       # record the current tag (empty output = runs latest)
```

Then set the new tag: edit the existing `CE_IMAGE_TAG=` line in `.env`, or add
that line if the install has none. Do not append a second line. Then pull and
start:

```bash
docker compose pull              # fetch the new image set BEFORE starting
# opt-in profiles you enabled with --profile are not in .env: repeat the flags, e.g. docker compose --profile stack-ingress pull, and the same flags on up -d
docker compose up -d
```

`docker compose up -d` runs `scripts/first-run-secrets.sh`-style key checks on
start: if the new compose requires a key `.env` lacks, `up` refuses and tells
you to run `scripts/first-run-secrets.sh`, which only fills in missing keys and
keeps every existing value byte-identical.

The one-shot `mod-migrate` service (container `my_ce_migrate`) runs as part of
`up -d` before `api` starts: it applies the release's migrations
(`CORE/API/migrations/*.py`, replayed from the append-only `release_steps`
ledger) and `api` cannot start until it exits 0. A failing migration blocks the
stack with a clear log line (`docker compose logs mod-migrate`), never a
half-upgraded API. Migrations are additive: new columns, new tables, new
settings rows. No hard deletes — existing rows, deprecated fields and old node
records stay on disk (the deprecation policy page covers how they are marked).

## Worker upgrade behaviour

- The embedded worker `mod-worker-embedded` (container `my_mod_worker`) is one of the composed services and
  follows the same `CE_IMAGE_TAG` pull as any other service.
- Remote workers update by pulling the new image tag
  (`docker pull ghcr.io/modtim/mod-worker:<new-tag>`, then restart the worker).
  CE v1.0.1 publishes no signed `mod-worker` binaries: the worker's self-update
  path fetches a `.sig` sibling and verifies it against the key compiled into
  the build, so with no signed artefacts published `update_available`
  stays False and nothing is fetched. A worker built from source has no key
  and cannot self-update.
  stays False and nothing is fetched.

## Verifying health afterwards

After `up -d`, confirm the same set yourself:

- `GET /health` on the API returns 200.
- The Dashboard and WorkflowBuilder containers are running.
- Dispatch is resumed: if you drained, `POST /v1/system/resume` (or the
  platform setting that pauses dispatch) must be cleared — the platform can be
  up on the new version and still drained.
- No stack left fenced: if the API was unreachable for longer than the worker
  fence window (about two minutes, per the restart window in the monthly patch
  runbook), workers self-fence their persistent stacks. Redeploy each fenced
  stack.

## Rollback

Rollback re-pins images. It is never done by editing a live container.

- Set `CE_IMAGE_TAG` back to the tag you recorded before the upgrade. Edit the
  existing `CE_IMAGE_TAG=` line in `.env` (do not append a second line), then
  run `docker compose pull` + `docker compose up -d`. The compose pins every CE
  image by that tag, so the previous release's images are what start.
- There is deliberately no automatic database rollback: migrations are
  additive and forward-compatible, so the old images run on the migrated DB.
  If you must restore an older DB, restore the `pg_dump` you took before the
  upgrade, per the backup runbook.

## What gets migrated

- `mod-migrate` is a one-shot service that `docker compose up -d` runs before
  `api` starts (there is no compose `migrate` profile in CE).
- Durable data (Postgres, Keycloak DB, the versitygw objects, the secrets store
  — OpenBao in Community Edition) is untouched by the upgrade itself; a `pg_dump`
  backup is a copy, not a move. If you ever follow the Resetting Keycloak (re-runs
  only) section of the README, all Keycloak users are lost, so back up the
  Keycloak database before you start.
