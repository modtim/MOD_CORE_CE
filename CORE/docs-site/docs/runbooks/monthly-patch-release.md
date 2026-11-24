---
sidebar_position: 63
slug: /runbooks/monthly-patch-release
title: Apply MOD's monthly patch release to your install
description: "Runbook: how an on-prem operator applies MOD's monthly patch drop to a running install — what the drop contains, the pre-checks (manifest, signature, minimum upgrade version, backup, drain), the apply sequence, how to verify the new image digests are actually running, how to roll the image pins back, and which parts of the patch cycle are not live yet."
---

# Apply MOD's monthly patch release to your install

Last reviewed: 2026-11-11

MOD ships **one patch drop per month**. A drop is a digest-pinned set of
platform images (in CE v1.0.1 they carry no cosign signature yet; signatures are
added by digest after release) plus the database migrations that go with them. The
dependency work behind a drop — dependency pull requests, base-image pin bumps,
package version bumps, the vuln scan review — happens on MOD's side. **Your**
job as the operator of an on-prem install is only to receive the result and
apply it to your running system.

Nothing applies a drop to your install by itself. On Community Edition the apply
step is plain `docker compose`: you pull the new image set, you recreate the
services, you confirm the result. **`update.sh` and `verify-release.sh` are not
in the CE extract** — they are dev-tree tools, and every flag this page names
belongs to them (see "Planned, not live yet" for what is missing).

## When to use

- A new release has been published for your edition and you run the install
  yourself.
- Your install has no network path to MOD's release registry and the drop
  arrives as an offline bundle you copy in yourself.
- A dependency CVE fix or an end-of-life base-image bump landed in the drop and
  you want to confirm it actually reached the images running on your boxes.
- You need the whole apply sequence written down: drain, migrate, recreate,
  health-check, undrain, redeploy fenced stacks.

Do **not** use this runbook to ship API code of your own — that is
[Restart the API without losing runs](/runbooks/api-restart-and-drain). Do not
use it for the operating system under the containers.

## Pre-checks (confirm each before starting)

Set `MOD_API_BASE` to your install's API URL (the one you open the Dashboard on;
on a local CE install that is `http://localhost:8000`).

1. **You have the release at a tag you can check out.** The CE apply path is
   `docker compose` against the tree you cloned at that tag: the compose file
   there carries the drop's image pins. Nothing in CE verifies a `release.json`
   signature — the verify tooling is not shipped (see "Planned, not live yet").
2. **Confirm the pins before you pull.** Read the `image:` lines in
   `docker-compose.yml`: upstream services are digest-pinned (`@sha256:`), the
   CE-authored ones interpolate
   `${CE_IMAGE_REGISTRY:-ghcr.io/modtim}/<name>:${CE_IMAGE_TAG:-latest}`.
   `docker compose config --images` prints what would actually be fetched (active profiles only; add your --profile flags for opt-in images).
3. **Your current version is upgradeable.** There is no `min_upgrade_from` gate
   in CE — nothing records a "last good version" and nothing refuses the pull.
   Confirm the migrations yourself instead: the drop's migrations ship inside
   the API image and are replayed by the one-shot `mod-migrate` service.
4. **Backup exists (do it yourself).** Take a `pg_dump` of the app database
   before you recreate anything — see
   [Back up an install and restore it onto a new box](/runbooks/ce-backup-and-restore).
   CE has no automatic pre-apply dump and no off-box backup target.
5. **Drain first (hard rule).** Stop dispatching new work, wait for in-flight
   runs and tasks to finish, **then** recreate. Never check after. There is no
   `--drain-timeout` and no `--force-drain` in CE: you poll the counters
   yourself (see [Restart the API without losing runs](/runbooks/api-restart-and-drain)).
   API downtime beyond roughly two minutes makes every worker self-fence the
   persistent stacks it hosts, so keep the window inside that.
6. **Admin token only if you undrain through the API.** `POST /v1/system/resume`
   clears the dispatch pause; it needs a platform-admin bearer token. There is
   no `--dry-run` preview in CE.
7. **Disk headroom.** The drop's images are pulled alongside the ones still
   running, so you need room for both until you run the cleanup step at the end.

## Steps

Drain first (the hard rule — see [Restart the API without losing runs](/runbooks/api-restart-and-drain)):
set the platform setting that pauses dispatch, poll until running workflow runs
and pending/running platform tasks are both zero, then:

```bash
# 1. Fetch the new image set. The compose file pins upstream images by digest
#    and interpolates ${CE_IMAGE_REGISTRY:-ghcr.io/modtim}/<name>:${CE_IMAGE_TAG:-latest},
#    so a drop lands by moving CE_IMAGE_TAG in .env (or by re-pinning the digests
#    in the compose file you checked out at the new tag).
docker compose pull
# opt-in profiles you enabled with --profile are not in .env: repeat the flags, e.g. docker compose --profile stack-ingress pull, and the same flags on up -d --wait

# 2. PostgreSQL major-version guard (read-only; run from the directory that holds
#    docker-compose.yml). Exit 1 = refused: the database volume was written by a
#    different PostgreSQL major than the new image. Any non-zero exit: stop here
#    and do not run step 3 (see the upgrade page's Pre-checks for the details).
scripts/pg-version-guard.sh -f docker-compose.yml

# 3. Recreate the services on the new images (this is the recreate step; the
#    one-shot mod-migrate service re-runs the drop's migrations on the API image).
docker compose up -d --wait

# 4. Remove the now-untagged old layers (not `-a`, which would also remove the images of optional profiles you have never started).
docker image prune -f
```

`docker compose up -d --wait` recreates the changed services in dependency
order, re-runs the one-shot `mod-migrate` service before the API starts, and
returns once every service with a healthcheck reports healthy. Then check
`/v1/health` returns 200. The images themselves are fetched by the separate
`docker compose pull` step above: upstream services by the digest pinned in the
compose file, the CE-authored ones (every `image:` line that uses
`${CE_IMAGE_REGISTRY}`) by the `CE_IMAGE_TAG` you set.

Workers follow separately. The embedded worker (service `mod-worker`, worker name `mod-worker-embedded`) is one of
the release services, so the recreate step covers it. Remote workers roll one at
a time — drain that worker, wait for its in-flight tasks, pull the new image tag
(CE v1: no signed worker binary is published, so CE workers update by pulling the
new image tag instead), wait for a heartbeat reporting the new version
and healthy — before the next one starts.

## Verify

```bash
# 1. Every service is running the image the compose file names (note the API
#    container is `my_fastapi_app`, not `my_mod_api`).
docker inspect --format '{{.Name}} {{.Image}}' my_fastapi_app my_mod_worker

# 2. The migrations from the drop ran (the one-shot service that replays them).
docker compose logs mod-migrate | tail -20

# 3. The platform is undrained and healthy.
curl -s -o /dev/null -w '%{http_code}' "$MOD_API_BASE/v1/health"   # expect 200
scripts/ce-doctor.sh                                               # expect all OK

# 4. Nothing is left near its memory rail or OOM-killed after the recreate.
scripts/ce-resize.sh auto
```

Confirm each:

- `docker compose up -d` exited 0 and every service reports healthy in
  `docker compose ps`.
- Every image id running on your services matches the `image:` line in the
  compose file you checked out at the new tag — not a tag you rebuilt locally.
- The migrations from the drop ran: `mod-migrate` logged them (step 2 above).
- No stack is left fenced: downtime stayed inside the two-minute worker
  self-fencing window, and any fenced stack was redeployed through
  [Restart the API without losing runs](/runbooks/api-restart-and-drain).
- The platform-settings dispatch pause from the drain step is cleared
  (`POST /v1/system/resume`).

## Rollback

There is no `--rollback` in CE. Roll back by hand, in the same drain window:

```bash
# Point the CE-authored images back at the previous tag, then recreate.
# opt-in profiles you enabled with --profile are not in .env: repeat the flags, e.g. docker compose --profile stack-ingress pull, and the same flags on up -d --wait
CE_IMAGE_TAG=<previous-tag> docker compose pull && CE_IMAGE_TAG=<previous-tag> docker compose up -d
```

This restores **image pins only**. It does not reverse database migrations, and
there is deliberately no database rollback: the drop's migrations are intended
to be additive, so an older image set normally still runs against the newer
database; if in doubt restore the `pg_dump` you took in pre-check 4. Nothing
records a "last good version" and nothing re-pins for you —
write the previous tag down before you apply a drop. If you need the pre-drop
database state, that is the `pg_dump` you took in pre-check 4 — see
[Back up an install and restore it onto a new box](/runbooks/ce-backup-and-restore).

If `docker compose up -d` fails partway, the services it already recreated run
on the new images and the rest are still on the old ones. Do not undrain until
you have decided which set you are running: finish the recreate or roll back,
then undrain once.

## Planned, not live yet

- **Automatic nightly re-scan of your stack base images filing findings for
  you.** Not wired. Until it lands, the scan review on MOD's side is a manual
  pass, and a drop can only contain what that pass found.
- **A MOD-side worker release script in this repo.** The worker binary is
  published through the object-storage release channel the API reads; there is
  no repo-side worker release script to run. Do not assume a path for one.
- **Floating base images pinned.** A few base images are still referenced
  `:latest`. They are tracked as findings and are not pinned yet, so a monthly
  drop does not currently bound those layers.
- **Custom patch cadence.** The drop is platform-wide; you cannot ask for a
  smaller drop or skip individual pins.
- **Anything that applies a drop without you.** There is no timer, daemon, or
  cron that updates your install; you run the compose commands above when you
  want the drop applied.
- **The apply/verify script surface.** Not in the CE extract at all: there is no
  `update.sh` (no `--bundle`, `--rollback`, `--dry-run`, `--drain-timeout`,
  `--force-drain`, `--backup-target`, `--roll-workers`, `--worker-drain-timeout`,
  `--worker-upgrade-timeout`), no `verify-release.sh` (no `--image`/`--blob`
  modes), no `MOD_RELEASE_OUT` consumer, and no code implements the manifest keys `min_upgrade_from` or
  `ref_by_digest`. Those are dev-tree tools; the CE equivalents are the
  `docker compose` commands on this page.

## Related

- [Upgrade and Rollback](/security/upgrade-and-rollback) — the same plain-`docker
  compose` apply sequence, as a reference page.
- [Restart the API without losing runs](/runbooks/api-restart-and-drain) — the
  drain rule and the worker self-fencing window.
- [Back up an install and restore it onto a new box](/runbooks/ce-backup-and-restore) —
  what the pre-drop backup covers.
