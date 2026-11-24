---
sidebar_position: 64
slug: /runbooks/ce-backup-and-restore
title: Back up an install and restore it onto a new box
description: "Runbook: every piece of state a Community Edition install keeps on disk, how to snapshot each one, why the secrets must be archived separately from the data, and how to bring the whole install up on different hardware so the same tenants, workflows, stored files and secrets come back."
---

# Back up an install and restore it onto a new box

Last reviewed: 2026-10-25

A Community Edition install is one directory (the **checkout root** of the CE
tree — the extract ships no `open-core/` directory; `docker-compose.yml` and
`.env` sit at that root), one `.env`,
the named volumes the compose file declares, and two host ciphertext
directories. If you lose the box —
dead hardware, a disk you cannot recover, a machine you are about to replace —
everything the tenants built is gone unless you took these pieces. This runbook
names each one, shows how to copy it, and shows how to bring it up on a
different machine.

This is about **moving an install**. It is not about
container stdout logs (see [Container log rotation](/runbooks/container-log-rotation)).

## What to back up, and why

| Piece | Lives in | How to copy it |
|---|---|---|
| `.env` — every secret (including `SECRET_ENCRYPTION_KEY`, `AUDIT_HMAC_KEY` and the four Keycloak client secrets), plus the `CE_*` size block | `<checkout root>/.env`, mode 0600 | plain `cp` — it is a file in your install directory |
| App database | volume `postgres_data`, mounted by service `postgres-app` at `/var/lib/postgresql/data` | `pg_dump -Fc` against `postgres-app` |
| Keycloak database | volume `keycloak_db_data`, mounted by service `keycloak-db` at `/var/lib/postgresql/data` | `pg_dump -Fc` against `keycloak-db` |
| Stored files and artifacts | host dir `${VERSITYGW_CIPHER_PATH:-/var/lib/mod/versitygw-ciphertext}/<tenant slug>` (the slug is `CE_TENANT_SLUG`, default `t1`), bound into `versitygw` at `/ciphertext-raw`; the store's `/data` is a gocryptfs mount inside the container | `sudo cp -a` the dir (it is root-owned `0700` after first start — the copy needs `sudo`) — it is a plain host directory, not a volume |
| versitygw IAM store | volume `versitygw-iam`, mounted by `versitygw` at `/iam` | volume tar, same way |
| OpenBao secret state | volume `openbao-data`, mounted by `openbao` at `/openbao/data` (single-node `raft` storage; the state dir is `/openbao/data/raft`) | volume tar — but the authoritative copy is the **raft snapshot** `vault-raft.snapshot`, written by `backup.sh` with `bao operator raft snapshot save`; prefer it over the raw volume tar |
| OpenBao unseal key + init marker | volume `openbao-init-state`, mounted rw by `openbao-init` at `/state`, read-only by `api` and `mod-worker` at `/run/openbao` | volume tar |
| OpenBao break-glass pair | volume `openbao-breakglass` (its entry in the compose top-level `volumes:` block), mounted only into `openbao-init` at `/breakglass`; holds the break-glass AppRole pair that the policy-upgrade branch of `first-run-openbao-init.sh` uses | volume tar, moved into the **secrets** archive with `openbao-init-state` |
| OpenBao audit device log | volume `openbao-audit`, mounted by `openbao` at `/openbao/logs` | volume tar (it grows; archive it, **never delete it**) |
| Valkey cache | host dir `${VALKEY_CIPHER_PATH:-/var/lib/mod/valkey-ciphertext}/data` (fixed `data` leaf, no tenant slug), bound into `redis` at `/ciphertext-raw` | **do not back up** — it is cache, heartbeats and pub/sub; it rebuilds |
| Worker outbox | volume `mod_worker_outbox` (`/var/lib/mod-worker/outbox`) on `mod-worker` (its `volumes:` in the compose `mod-worker` service) | volume tar, same way: it holds task results that could not be delivered yet |
| Worker stack state | volume `mod_worker_stacks` (`/var/lib/mod-worker/stacks`) on `mod-worker` (its `volumes:` in the compose `mod-worker` service) | volume tar, same way: it is small, and holds the worker's restart-intent record for your stacks plus the stack secrets markers (not user data); include it in volume backups |
| Worker binary | volume `mod_worker_bin` (`/var/lib/mod-worker/bin`) on `mod-worker` (its `volumes:` in the compose `mod-worker` service) | **do not back up** — the binary updates by image pull (the `mod_worker_bin` entry in the compose top-level `volumes:` block says so), so it is not state |
| Node builder state | volume `buildkit-state` (profile `builder`, the `buildkitd` service's `volumes:` in compose; the rootless BuildKit layer/cache store, GC-capped by `CE_BUILDKIT_CACHE_MB`) | **do not back up** — it is a cache and rebuilds |
| Registry and stack-ingress edge (profiles `registry` and `stack-ingress` only) | volumes `registry-data`, `registry-auth`, `registry-certs` (the `registry-*` entries in the compose top-level `volumes:` block) and `caddy-data`, `caddy-config` (the `caddy-*` entries in that same block) | volume tars in the profile block under the backup loop; only if you enabled those profiles |

> **Keep these 2 pieces offline and separate from the data archive:**
> `.env` and the `openbao-init-state` tarball. Lose `.env` and stored secrets
> cannot be unwrapped (`SECRET_ENCRYPTION_KEY`), the hashes recorded on new
> audit rows no longer match the old ones (`AUDIT_HMAC_KEY`), and the Keycloak
> client secrets stop matching. Lose the tarball and `openbao-init` cannot unseal
> the secret store.
>
> The **gocryptfs passphrase file** (`/var/lib/mod/gocryptfs-pass`, mode `0600`)
> is the third must-keep, but it does **not** travel apart: lose it and **the
> stored files cannot be recovered at all** — the ciphertext on disk is useless
> without it, and nothing regenerates it. (`/run/secrets/gocryptfs-pass` is only a copy that the stack rebuilds from it after a reboot; back up the file under `/var/lib/mod`.) So it goes in the **same archive as the
> ciphertext** (the data archive), never in the secrets archive on its own.
>
> **Security callout — split the archive.** `.env` and `openbao-init-state` are
> secrets, and together they unlock everything else: `.env` holds the Postgres,
> Keycloak and versitygw root credentials and the keys that decrypt stored
> secrets, and `openbao-init-state` holds the single local key that unseals the
> secret store. The unseal key is deliberately kept in its own volume, apart
> from the secret data, so that no single archive of the data can open the
> secrets. Keep that split when you copy: put `.env` and `openbao-init-state` in
> a **separate** archive, stored in a different place, with tighter access than
> the data archive. What the data archive alone cannot do is open the secret
> store or unwrap the stored secrets — it has no unseal key and no
> `SECRET_ENCRYPTION_KEY`. What it **can** do is read the tenant's stored files
> back, because it carries the ciphertext together with the passphrase it needs:
> the passphrase and the ciphertext must stay in one archive (or in two archives
> that are always stored together), so treat the data archive as sensitive too
> and give it the same tight access. Never archive the passphrase apart from the
> ciphertext, and never let one of the two archives end up without the other.

## OpenBao audit log

The OpenBao audit device writes `/openbao/logs/audit.log` inside the `openbao` container; that path is the `openbao-audit` volume. The file only grows: OpenBao adds a line for every request it serves, and the CE build never deletes or rotates it. To archive it while the box runs:

1. Copy it out with the compose service name, from the install directory: `docker compose cp openbao:/openbao/logs/audit.log ./openbao-audit-<date>.log`.
2. Verify the copy: it is non-empty and its last line is a complete line.
3. Store the copy with the other backup archives, with the same care, because audit lines name users and paths.

Do not truncate or edit `audit.log` by hand while OpenBao runs. OpenBao writes to the file while it runs, so a hand edit can cut or corrupt the audit trail.

## Back up

Run everything from inside the **CE checkout root** (that is where `docker compose`
finds `docker-compose.yml` and `.env`; the CE extract has no `open-core/`
directory).

Stop the writers first so the snapshot is consistent — the databases stay up,
because `pg_dump` runs against them, and every writer of a volume or host
directory you are about to archive must be stopped **before** the archive is
taken:

```bash
docker compose stop api mod-worker mod-dashboard workflow-builder keycloak versitygw openbao
# Only if you enabled these profiles (the services exist only with their
# profile; --profile makes compose resolve them; a stopped or absent one is a no-op):
docker compose --profile builder --profile registry --profile stack-ingress \
  stop buildkitd buildkitd-net registry caddy
```

`versitygw` writes the `versitygw-iam` volume and the tenant's ciphertext dir;
`openbao` writes `openbao-data` and `openbao-audit`. Leaving either running
while you tar its volume is the mistake this step exists to prevent.

Then take the pieces:

```bash
mkdir -p "<backup-dir>/data" "<backup-dir>/secrets"
cp -p .env "<backup-dir>/secrets/.env"

docker compose exec -T postgres-app \
  sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  > "<backup-dir>/data/app-db.dump"

docker compose exec -T keycloak-db \
  sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  > "<backup-dir>/data/keycloak-db.dump"
```

The volume tarballs below (and the checksum step in Verify) run a bare
`alpine` container. No service in `docker-compose.yml` uses the `alpine`
image, so a computer with no internet may not have it. Before you start, on a
machine with internet, pull it once: `docker pull alpine`. Without a local copy
the `docker run` steps fail, because Docker tries to pull it.

```bash
# Compose names each volume <project>_<name>; this compose file sets
# `name: mod-core-ce`, so the prefix is `mod-core-ce` no matter what the
# install directory is called. If you ran compose with -p <name> or set
# COMPOSE_PROJECT_NAME in .env, use that name.
PREFIX=mod-core-ce
for vol in versitygw-iam openbao-data openbao-init-state openbao-breakglass openbao-audit mod_worker_outbox mod_worker_stacks; do
  docker run --rm -v "${PREFIX}_${vol}":/data -v "<backup-dir>/data":/backup alpine \
    tar czf "/backup/${vol}.tar.gz" -C /data .
done
```

Only if you enabled the `registry` or `stack-ingress` profile, archive its volumes
the same way (they exist only with those profiles):

```bash
for vol in registry-data registry-auth registry-certs caddy-data caddy-config; do
  docker run --rm -v "${PREFIX}_${vol}":/data -v "<backup-dir>/data":/backup alpine \
    tar czf "/backup/${vol}.tar.gz" -C /data .
done
```

Confirm the names before you trust the tarballs — every volume in the loops above
must appear, each with the prefix you used:

```bash
docker volume ls | grep -E 'openbao|versitygw-iam|mod_worker_outbox|mod_worker_stacks|registry-|caddy-'
```

Move the two tars that belong with the secrets into their archive —
`openbao-init-state.tar.gz` and `openbao-breakglass.tar.gz` go with `.env`, not with the data:

```bash
mv "<backup-dir>/data/openbao-init-state.tar.gz" "<backup-dir>/data/openbao-breakglass.tar.gz" "<backup-dir>/secrets/"
```

The stored files are **not** a volume any more — they live in a host directory of
ciphertext. Copy it as a plain directory, and copy the passphrase file that belongs
to it. Never print or `cat` the passphrase, and never let it end up apart from the
ciphertext it unlocks:

```bash
# CE_TENANT_SLUG and VERSITYGW_CIPHER_PATH are compose/.env variables, not
# shell variables: a bare shell sees them empty and silently takes the `t1`
# default even on a renamed tenant. Read them out of .env.
BASE="$(grep -sE '^VERSITYGW_CIPHER_PATH=' .env | cut -d= -f2-)"
BASE="${BASE:-/var/lib/mod/versitygw-ciphertext}"
SLUG="$(grep -sE '^CE_TENANT_SLUG=' .env | cut -d= -f2-)"
CIPHER_DIR="$BASE/${SLUG:-t1}"
mkdir -p "<backup-dir>/data/ciphertext" "<backup-dir>/secrets"
# The cipher dir is 0700 root-owned and the passfile lives under /var/lib/mod (beside the cipher dirs)
# at 0600 root, so both copies need sudo (the compose commands above must NOT
# get sudo).
sudo cp -a "$CIPHER_DIR" "<backup-dir>/data/ciphertext/versitygw-<tenant>"
sudo cp -a /var/lib/mod/gocryptfs-pass "<backup-dir>/data/ciphertext/gocryptfs-pass"
```

The passphrase goes **into the data archive, next to the ciphertext it unlocks**
— never into the secrets archive on its own (see the callout above).

The `gocryptfs.conf` file that a store needs to open it lives **inside each
cipher dir** (`first-run-sidecar.sh` names it as `$CIPHER_DIR/gocryptfs.conf`
for the versitygw store). The `cp -a "$CIPHER_DIR"` above carries it along with
the ciphertext, so do not copy the files out of that dir one by one.

The Valkey cache dir is deliberately not copied (it rebuilds), so its store,
and its own `gocryptfs.conf`, starts empty on the new box. If you set
`VERSITYGW_CIPHER_PATH` to something other than the default, copy that path
instead.

Record the unseal-key hash (the restore Verify step compares against it; the
key itself is not printed) and a checksum list the restore checks first:

```bash
docker run --rm -v "${PREFIX}_openbao-init-state":/data:ro alpine \
  sha256sum /data/unseal_key > "<backup-dir>/secrets/unseal_key.sha256"
(cd "<backup-dir>" && find data secrets -type f -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
```

Bring everything you stopped back up (the same service names you stopped; start
the profile services only if you stopped them above):

```bash
docker compose start api mod-worker mod-dashboard workflow-builder keycloak versitygw openbao
docker compose --profile builder --profile registry --profile stack-ingress \
  start buildkitd-net buildkitd registry caddy
```

**Online variant (no stop).** The two `pg_dump`s are safe on a running stack —
they take a read-only, consistent snapshot of the database. The volume tars are
not: `versitygw-iam` is being written while uploads and task runs are live,
`openbao-data` changes whenever a secret is minted or rotated, and `openbao-audit`
grows on every OpenBao operation. Tar those only with their writers stopped, as
above. The same applies to the ciphertext dir:
`sudo cp -a` it only while uploads are stopped, and never mid-write.

## The gocryptfs passphrase: keep a root-only copy outside the data volume

The passphrase file `/var/lib/mod/gocryptfs-pass` (mode `0600`, root-owned) is the
**only** thing that opens the encrypted object store. Without it the ciphertext on
disk cannot be read at all, and nothing regenerates it: `scripts/first-run-sidecar.sh`
refuses to write a new key while encrypted data exists, and a different passphrase
makes that data permanently unreadable. `/run/secrets/gocryptfs-pass` is only a copy the
stack rebuilds from the file under `/var/lib/mod` after a reboot, so back up that
disk-backed file, never the `/run` copy.

The data archive above carries the passphrase **inside** the data volume archive,
which is the right shape for a restore. It is not a safety net: if that archive
lands on the same failing disk, or is lost with the box, the passphrase is gone with
it. So take one **root-only copy outside the data volume** once, right after install,
and keep it off-box under the operator's own secret handling:

```bash
# once, after install; /root/mod-backup is beside nothing the stack writes -
# it is NOT under the data volume, the cipher dirs or the checkout root
sudo mkdir -p /root/mod-backup
sudo install -m 600 -o root -g root /var/lib/mod/gocryptfs-pass /root/mod-backup/gocryptfs-pass
```

Copy that file off the box the same way you move the secrets archive (the operator's
own channel: their own scp/usb/keystore, never a shared mount), and keep it with the
same care as `.env`. Never commit it, never paste it, never print it, and never put
it in a world-readable location: mode `0600` and root ownership are the whole point.

To restore after a reboot or a lost box, put it back in place first, then let the
scripts do their normal job:

```bash
sudo install -m 600 -o root -g root /root/mod-backup/gocryptfs-pass /var/lib/mod/gocryptfs-pass
sudo bash scripts/recover-after-reboot.sh
```

`scripts/recover-after-reboot.sh` rebuilds the `/run/secrets/gocryptfs-pass` copy from
the file under `/var/lib/mod` and restarts the encrypted stores; on a fresh install
instead, `scripts/first-run-sidecar.sh` does the same copy. If both key files exist
and hold different passphrases, both scripts refuse to choose — restore the one the
data was created with and remove the other.

## Restore onto a new box

1. Install Docker and Docker Compose on the new box.
2. Get the same release: clone the CE tree at the **same tag** the old install
   ran, so `CE_IMAGE_TAG` in the compose default still names the images that
   box ran. Do not let it fall back to `latest`. Write the tag down
   when you take the backup (record the tag from `docker compose ps --format
   '{{.Image}}'` — that is what is running; `docker compose images` only lists
   what the compose file references): the
   dumps match that release's schema, so restore onto the same release first and
   upgrade afterwards with [Upgrade and rollback](/security/upgrade-and-rollback).
3. Copy `.env` into the **CE checkout root** **before the first boot**. This is
   the step that preserves the secrets: `scripts/first-run-secrets.sh` checks
   whether every infra key is already in `.env` and, if it is, prints
   `first-run-secrets: <path> already has every infra key — nothing generated.`
   and exits without generating anything (the script is per-key idempotent: any
   key the file lacks is still filled in, so a partial `.env` is not silently
   skipped). Re-running adds the `CE_*` sizing block only if it is absent;
   an existing block is left alone, and any `CE_*` you already set stays yours. Miss this step and the
   box mints a fresh set of credentials, and every stored file, Keycloak client
   secret and encrypted secret from the old box stops matching.
   Then set `PUBLIC_HOST` in that same `.env` to the name or IP this box will be
   reached at, **before the first `docker compose up`**. The restored value is
   the old box's host. Compose passes it to `keycloak-admin-init` as
   `CE_PUBLIC_HOST` (`docker-compose.yml`, the `keycloak-admin-init`
   service), and `keycloak/converge-client-origins.sh` adds that host to the
   three realm clients' redirect URIs and web origins on every (re)create of
   `keycloak-admin-init`, always keeping the localhost entries. If the old box
   was reachable over the LAN, also set `CE_BIND_ADDR` as the install did (it
   defaults to `127.0.0.1`; `scripts/install.sh --lan` sets it). Without a
   matching `PUBLIC_HOST`, log-in works only on localhost.

   The Keycloak realm is not stored in a Keycloak volume. Its rows live in the
   `keycloak-db` database (`keycloak_db_data`), which step 5 restores from
   `keycloak-db.dump`, so the restored realm keeps the old box's client
   origins until `keycloak-admin-init` converges them as above. Only a
   keycloak-db that does not have the realm yet (one you did not restore from
   the dump) imports it from `keycloak/core-realm.json`, because
   `--import-realm` imports only a realm that does not exist.
4. The volume tars you made go into volumes that do not exist yet on this box —
   `docker run -v <name>:/data` creates a missing named volume, so the loops
   below both create and fill each one. Do this before any service that
   mounts them has started, in this order: `versitygw-iam`, `openbao-data`,
   `openbao-audit`, `openbao-init-state`, `openbao-breakglass`,
   `mod_worker_outbox`, `mod_worker_stacks`, then the profile volumes if you archived them. The stored files are not a volume:
   unpack the ciphertext dir into the host path the compose binds, and put the
   passphrase file back at `/var/lib/mod/gocryptfs-pass` (mode `0600`, owner
   root) **before** any store starts — the image entrypoint refuses to mount
   without it. Then run `scripts/first-run-sidecar.sh` (it copies it to
   `/run/secrets/gocryptfs-pass` for the stores); after a later reboot
   `scripts/recover-after-reboot.sh` does that copy by itself. Both of those
   host paths are root-owned (`0700` dir, `0600` file), so do them with
   `sudo`. Put the passphrase file back first. Then unpack the ciphertext into the new
   box's versitygw tenant cipher dir **before** you run
   `scripts/first-run-sidecar.sh`: the script creates any cipher dir that is missing,
   and the `cp -a` below would then nest the copy inside the empty dir. Only that dir
   is restored. The Valkey `data` dir
   (`${VALKEY_CIPHER_PATH:-/var/lib/mod/valkey-ciphertext}/data`) is not in the backup
   (it is cache, see the backup table): its store starts empty and rebuilds. Without the
   passphrase file the script refuses to run over existing ciphertext. The script
   refuses to write any key while encrypted data is there. It prints
   `the encrypted data already exists ($CIPHER_DIR or $VALKEY_CIPHER_DIR) but its key file is missing: expected /var/lib/mod/gocryptfs-pass (or the copy /run/secrets/gocryptfs-pass). Restore that passphrase file from your backup (mode 0600, root-owned) and run this script again. Do NOT generate a new one: a new passphrase would make the existing encrypted data unreadable forever. Nothing was created or changed.`
   and changes nothing. Do not unpack into a dir that already holds a store.

   Keep a second copy of the backup off the box before you restore. Then check
   the backup is intact: `cd <backup-dir> && sha256sum -c SHA256SUMS` must
   print `<file>: OK` for every file, or STOP. Every **backup-side** mount below
   is read-only (`:ro`); the volume you unpack **into** is writable, so a
   `czf`-for-`xzf` slip can still rewrite that volume — it cannot rewrite the
   backup tars.

   ```bash
   PREFIX=mod-core-ce   # the compose project name, not the directory name
   for vol in versitygw-iam openbao-data openbao-audit mod_worker_outbox mod_worker_stacks; do
     docker run --rm -v "${PREFIX}_${vol}":/data -v "<backup-dir>/data":/backup:ro alpine \
       tar xzf "/backup/${vol}.tar.gz" -C /data
   done
   for vol in openbao-init-state openbao-breakglass; do
     docker run --rm -v "${PREFIX}_${vol}":/data -v "<backup-dir>/secrets":/backup:ro alpine \
       tar xzf "/backup/${vol}.tar.gz" -C /data
   done
   ```

   Only the profile tars you actually made (the profile block of the backup):

   ```bash
   for vol in registry-data registry-auth registry-certs caddy-data caddy-config; do
     [ -f "<backup-dir>/data/${vol}.tar.gz" ] || continue
     docker run --rm -v "${PREFIX}_${vol}":/data -v "<backup-dir>/data":/backup:ro alpine \
       tar xzf "/backup/${vol}.tar.gz" -C /data
   done
   ```

   The stored files (ciphertext) are not a volume. Copy the archived dir to the host
   path compose binds (the `versitygw` service's `volumes:` bind to the ciphertext dir:
   `${VERSITYGW_CIPHER_PATH:-/var/lib/mod/versitygw-ciphertext}/${CE_TENANT_SLUG:-t1}`),
   reading the same two values from `.env` that the backup read. Do it before the first
   `docker compose up`, or Docker creates that bind source as an empty root directory.
   The target must not exist yet (`cp -a` would nest a copy inside an existing dir),
   and must not already hold data: `first-run-sidecar.sh` refuses to create a key over
   existing encrypted data (the refusal in `first-run-sidecar.sh`).

   ```bash
   BASE="$(grep -sE '^VERSITYGW_CIPHER_PATH=' .env | cut -d= -f2-)"
   BASE="${BASE:-/var/lib/mod/versitygw-ciphertext}"
   SLUG="$(grep -sE '^CE_TENANT_SLUG=' .env | cut -d= -f2-)"
   sudo mkdir -p "$BASE"
   sudo cp -a "<backup-dir>/data/ciphertext/versitygw-<tenant>" "$BASE/${SLUG:-t1}"
   sudo cp -a "<backup-dir>/data/ciphertext/gocryptfs-pass" /var/lib/mod/gocryptfs-pass
   sudo chown root:root /var/lib/mod/gocryptfs-pass && sudo chmod 0600 /var/lib/mod/gocryptfs-pass
   ```

5. Start the databases only, then restore them. `pg_restore` cannot read the
   dump from the container's stdin and write its own report at the same time,
   so stage the dump inside the container first, then restore it:

   On a new box the containers do not exist yet, so use `up -d` (not `start`):

   ```bash
   docker compose up -d postgres-app keycloak-db
   docker compose exec -T postgres-app sh -c 'cat > /tmp/restore.dump' \
     < "<backup-dir>/data/app-db.dump"
   docker compose exec postgres-app \
     sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore --clean --if-exists \
            -U "$POSTGRES_USER" -d "$POSTGRES_DB" /tmp/restore.dump'
   ```

   Repeat both steps against `keycloak-db` with `keycloak-db.dump`.
6. Put `openbao-init-state`, `openbao-breakglass` and `openbao-data` in place **before** `openbao`
   starts. The one-shot `openbao-init` service refuses to run over stale state
   and refuses to unseal without its key file: if the data volume arrives
   without the state volume it fails loudly with
   `ERROR: OpenBao is already initialized but no local unseal key file
   ($UNSEAL_KEY_PATH) was found in the openbao-init-state volume. Cannot
   auto-unseal. Restore that volume or re-init.` (`$UNSEAL_KEY_PATH` is the
   script's variable; it prints the real key-file path.) That is the whole reason the two are archived
   separately — and it is also your proof that you restored them together.
7. `docker compose up -d`, then verify.

## Verify

- [ ] Log in on the new box with the tenant that existed on the old one.
- [ ] Log in from another computer at the `PUBLIC_HOST` address you set in
      `.env`, not only on localhost. If it fails, check `PUBLIC_HOST` and the
      `CE_BIND_ADDR` setting, then read the `keycloak-admin-init` logs: origin
      convergence reports a failure as a `WARN` line and does not stop login.
- [ ] The workflow list is present — the workflows they built are still there.
- [ ] A file that was stored before the move downloads (this proves
      the versitygw ciphertext dir, the passphrase file and the versitygw
      credentials all came back — a missing passphrase means the store
      starts empty, not broken).
- [ ] Audit records written before the move are still there: open one through
      `GET /v1/audit-logs/permissions/{log_id}`. Community Edition records a hash on
      every audit row but ships no verification, so this checks the data came
      back, not the hash chain. Check that the newest audit row from before the
      wipe is still present,
      and the row count is at least the pre-wipe count (the restored stack
      writes a few rows when it boots). Do not compare the max id: it grows.
- [ ] OpenBao came up with the RESTORED keys, not a fresh init. An empty
      `openbao-init-state` makes the stack re-initialise and still shows
      `Initialized true` / `Sealed false`, so compare the unseal-key hash with
      the one recorded at backup (the key itself is never printed):

      ```bash
      docker run --rm -v "${PREFIX}_openbao-init-state":/data:ro alpine \
        sha256sum /data/unseal_key | cut -d' ' -f1 \
        | diff - <(cut -d' ' -f1 "<backup-dir>/secrets/unseal_key.sha256") \
        && echo "unseal key matches backup"
      ```

      Expected: `unseal key matches backup`. A diff means the stack
      re-initialised: stop, wipe the new volumes and redo the restore. This
      proves the key file is the backed-up one; the stored-secret read-back
      below proves it belongs to `openbao-data`.
- [ ] `scripts/ce-doctor.sh` reports nothing near its memory cap and nothing
      OOM-killed.
- [ ] `docker compose ps` shows every service `healthy`, including `openbao`
      (unsealed) and `openbao-init` exited 0.
- [ ] A secret that was stored before the move still reads back — this proves
      the unseal key and the OpenBao data volume belong to each other.

## Different box, different size

The `CE_*` memory rails in `.env` were chosen for the old box's RAM. If the new
box differs, let the sizing script re-read the hardware — it rewrites only its
managed block in `.env` (the `# >>> ce-size-profile` block) and keeps any `CE_*`
manual override you put below that block:

```bash
scripts/ce-resize.sh auto
```

Run it before `docker compose up -d`, or restart the services after it.
