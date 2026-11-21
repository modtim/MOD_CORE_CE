---
sidebar_position: 66
slug: /runbooks/uninstall-ce
title: Stop, remove or fully uninstall a Community Edition install
description: "Runbook: the three levels of taking a Community Edition install down - stop only, remove the containers but keep the data, and remove everything - with exactly what each command deletes, what survives on the host, and what to copy first."
---

# Stop, remove or fully uninstall a Community Edition install

Last reviewed: 2026-11-12

A Community Edition install keeps its state in three places: the named Docker
volumes, two ciphertext directories on the host, and one key file on the host.
`docker compose down -v` removes only the first of these. This page shows what
each level of teardown deletes and what it leaves behind.

Run every `docker compose` command below from the **checkout root** (the
directory with `docker-compose.yml` and `.env`). The compose file sets
`name: mod-core-ce`, so every volume is named `mod-core-ce_<name>` no matter
what the directory is called.

## Before you wipe anything

If you may want the data again, take a backup first, using
[Back up an install and restore it onto a new box](/runbooks/ce-backup-and-restore).
That page lists each piece of state and how to copy it. Once the volumes are
gone nothing can bring them back.

## Level 1: stop only (everything is kept)

```bash
docker compose down
```

This stops and removes the containers and the network. It does **not** touch
any volume, any host directory, the key file or `.env`. To start again:

```bash
docker compose up -d
```

(or re-run `scripts/install.sh`, which is safe to re-run and keeps your
answers and passwords).

## Level 2: remove the containers, keep the data

Level 1 already does this. After `docker compose down` these all remain:

- every named volume (`docker volume ls` lists them with the `mod-core-ce_`
  prefix),
- the checkout directory and its `.env` (all generated secrets),
- the ciphertext directories and the key file on the host (see Level 3).

Nothing is lost, and `docker compose up -d` brings the same install back.

### Profile services

Some services are profile-gated: `buildkitd-net` and `buildkitd` (profile
`builder`), `caddy` (profile `stack-ingress`), `video-converter-h264` and
`ffmpeg-cpu-h264` (profile `h264`), and `registry-init` and `registry`
(profile `registry`). `scripts/install.sh` enables the node builder by writing
`COMPOSE_PROFILES=builder` into `.env`, and Compose reads that line for `down`
as it does for `up`. A profile that is **not** enabled in `.env` is not part of
the command, so its containers are left running. If you started one by hand
with `--profile registry`, pass the same flag when you take it down:

```bash
docker compose --profile registry down
```

## Level 3: remove everything

> **Warning.** The ciphertext directories and the key file are separate from
> the Docker volumes and survive `docker compose down -v`. The ciphertext is
> useless without the key, and nothing can regenerate the key. Delete the key
> and keep the ciphertext, or delete the ciphertext and keep the key, and the
> stored files are unrecoverable. Remove both together or neither.

### 1. Remove the containers and the named volumes

```bash
docker compose down -v
```

`-v` deletes the named volumes the compose file declares (15 in this release).
Consequences, by volume:

| Volume | What is lost |
|---|---|
| `postgres_data` | the application database: tenants, users, workflows, run history |
| `keycloak_db_data` | the Keycloak database: logins and realm settings |
| `versitygw-iam` | the stored-files service's minted accounts |
| `openbao-data` | the secret store's data |
| `openbao-init-state` | the **unseal key** for the secret store |
| `openbao-breakglass` | the break-glass credentials for the secret store |
| `openbao-audit` | the secret store's audit log (archive it first if you need it) |
| `mod_worker_outbox` | task results that were waiting to be delivered |
| `mod_worker_bin` | the persistent worker binary (the worker image provides it again on the next start) |
| `buildkit-state` | the node builder's layer cache (rebuilt on demand) |
| `registry-data`, `registry-auth`, `registry-certs` | the optional local registry's images, password file and certificates |
| `caddy-data`, `caddy-config` | the optional TLS edge's certificates and saved config |

`openbao-data` and `openbao-init-state` go together: deleting either one makes
the other useless, so the stored secrets are gone for good.

If any of the optional profiles are running, add the same `--profile` flags
as in Level 2 so their containers and volumes are removed too.

### 2. Remove the host directories (stored files and cache)

These live outside Docker, are owned by root and are not removed by
`docker compose down -v`. The defaults, from the compose file, are:

```bash
# stored files, one directory per tenant (default tenant slug: t1)
sudo rm -rf /var/lib/mod/versitygw-ciphertext
# the Valkey cache directory (a cache; it rebuilds)
sudo rm -rf /var/lib/mod/valkey-ciphertext
```

If you set `VERSITYGW_CIPHER_PATH` or `VALKEY_CIPHER_PATH` in `.env`, remove
those directories instead. Read the values out of `.env` first; they are
compose variables, not shell variables.

### 3. Remove the key file

```bash
sudo rm -f /var/lib/mod/gocryptfs-pass
```

This is the passphrase the stored files were encrypted with. Delete it only
together with the ciphertext in step 2, and only after you have decided you
will never need the files again. If you keep the ciphertext, keep this file
too.

### 4. Remove the install directory

Delete the checkout directory last. It holds `.env`, which contains every
secret generated for this install (database and Keycloak passwords, the key
that decrypts stored secrets, the audit key and the client secrets).
`.env` is mode `0600`: delete it rather than copying it around.

## A fresh install after a full removal

After Level 3, start from an empty directory and run `scripts/install.sh`
again. It generates new secrets and a new key. `scripts/first-run-sidecar.sh`
refuses to generate a key when ciphertext from a previous install is still on
disk, because a new key would make that data permanently unreadable. If you see
that refusal, either restore the original key file or finish step 2 above.
