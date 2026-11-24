# Install guide: prerequisites and quickstart

Everything behind the three-step start on the front page (usually 10-20 minutes on a first run, longer on a slow connection): what to check first, the full install, choosing images and resetting Keycloak. [Back to the README](../README.md).

## Prerequisites

Check these before the first `docker compose up`; the numbers below are derived
from this compose file, not guessed.

- **OS:** Linux x86_64 (amd64) is the supported platform. macOS and Windows
  (WSL2) through Docker Desktop are untested: `mod-worker` bind-mounts the host's
  `/var/run/docker.sock` (the `mod-worker` service's `docker.sock` volume mount)
  and runs as `user: "0:<DOCKER_GID>"`, so you need a Linux-compatible Docker
  socket path and GID, and the encrypted-store folders need a root-owned host path.
- **Shell:** **bash** is required for `scripts/first-run-secrets.sh`,
  `scripts/ce-resize.sh` and `scripts/ce-doctor.sh` (they use bash-only syntax).
  Alpine / `docker:dind` have no bash — `apk add bash` first (or run the install
  on a bash host); `keycloak/ensure-client-secrets.sh` is plain `/bin/sh` and
  needs none. Running one of the bash scripts under `sh` now fails loudly (exit
  2, "needs bash") instead of writing `.env` to the wrong directory.
- **Docker:** rootful Docker; your user is in the `docker` group. Run the
  scripts and `docker compose` as that normal user, not with `sudo` (a sudo
  first run leaves `.env` owned by root and later steps then fail on it).
  Docker Engine with **Compose v2** (`docker compose`, one word —
  the legacy `docker-compose` v1 binary does not understand this file).
  Distro `docker.io` packages do NOT include the Compose v2 plugin: install
  Docker from Docker's own apt repo (`docker-ce` + `docker-compose-plugin`), or
  add the distro plugin package (Ubuntu: `docker-compose-v2`; Debian:
  `docker-compose`). Check with `docker compose version`. The
  minimum I can justify from the features actually used: the top-level
  `name: mod-core-ce` project key and `depends_on: {condition: service_healthy
  | service_completed_successfully}` (used across the `api`, `mod-worker`,
  `mod-dashboard` and `workflow-builder` services) are Compose-Specification features, so any
  **current Docker Engine with a current Compose v2 plugin**
  works; no `include:` or other recent Compose-only features are used. Nothing
  in `scripts/` checks the versions, so an Engine that is too old shows up as a
  `docker compose` parse error, not a CE message. Tested with the Engine and
  Compose bundled in the `docker:dind` image of the 2026-10 clean-room install
  (exact version not recorded).
- **RAM:** 8 GB+ recommended to run nodes. Below 7 GB of RAM the first-run script picks the small sizing profile. It enforces no lower limit; a 4 GB computer is tight, so use 8 GB or more. You do not pick the memory rails yourself: `scripts/first-run-secrets.sh`
  reads the RAM available to the stack (the cgroup limit when it is lower than the
  host's `/proc/meminfo`, so a 4 GB Docker VM sizes `small` even on a 125 GB host)
  and appends that profile's `CE_*_MEM` rails to `.env`. Every `mem_limit` for a
  service that starts by default is `${CE_*_MEM:-…}`; the one exception is the
  `builder` profile's `buildkitd-net` service, pinned at `0.03g` (its `mem_limit` in
  `docker-compose.yml`), which no `CE_*` rail raises.

  | RAM seen by the stack | Profile `first-run` picks | What it writes |
  |---|---|---|
  | under 7 GB | `small` | the 4 GB / 2 vCPU rails (`profiles/small.env`) |
  | 7 GB to under 24 GB | `medium` | `profiles/medium.env` |
  | 24 GB and up | `large` | `profiles/large.env` |

  Thresholds are the size-profile block in `scripts/first-run-secrets.sh` (`< 7 GB ->
  small`, `< 24 GB -> medium`, else `large`). Override the pick with
  `CE_SIZE=small|medium|large` on the first run, or change the rails afterwards
  with `scripts/ce-resize.sh` (see the ce-doctor note below).

  The `small` rails are memory *caps*, not preallocation, and their sum for the
  long-running services is about 5.4 GB (the 11 long-running rails in
  `profiles/small.env`; the one-shot init rail adds 0.25 GB more, and the node
  builder adds more), so a 4 GB box has little headroom: expect swapping or
  restarts when nodes run.
  - **Service keeps restarting?** Run `scripts/ce-doctor.sh` — it reads the
    running CE containers and tells you which one is near its memory cap or
    was OOM-killed, and prints the exact `scripts/ce-resize.sh` command to
    fix it (read-only; it never starts, stops or restarts anything).
  - **Moving this install to another box?** See
    `CORE/docs-site/docs/runbooks/ce-backup-and-restore.md` (shipped in the CE
    docs site as [Back up an install and restore it onto a new box](../CORE/docs-site/docs/runbooks/ce-backup-and-restore.md)) —
    what to snapshot, and how to bring it up on different hardware.
- **Free disk ≥ 20 GiB (about 21 GB).** The compose file declares **27** services: **22** start
  by default **when the node builder is on (the install script turns it on; 20 with it
  off)** (14 long-running — `postgres-app`, `redis`, `mailpit`, `versitygw`,
  `openbao`, `openbao-unseal`, `keycloak-db`, `keycloak`, `api`, `mod-worker`, `mod-dashboard`,
  `mod-docs`, `workflow-builder`, `mcp-gateway` — the **node builder** pair `buildkitd-net` and
  `buildkitd`, and 6 one-shot jobs which exit when done:
  `openbao-data-perms`, `openbao-init`, `keycloak-admin-init`, `mod-migrate`, `db-owner-repair`,
  `ce-node-repair`), and **5 are profile-gated** and stay down unless you opt
  in: `caddy` (`stack-ingress`, the TLS/login edge), `video-converter-h264` and
  `ffmpeg-cpu-h264` (the `h264` add-on pair), `registry-init` and `registry`
  (the `registry` profile). The node builder is **ON by default from day 1**:
  `scripts/install.sh` writes `COMPOSE_PROFILES=builder` into `.env` (it never
  touches a value you already set, and never enables `stack-ingress` or
  `registry` for you). Turn the builder off by editing that line in `.env`
  (leave `COMPOSE_PROFILES` empty or set it to your own list without `builder`);
  opt in to the other two with `docker compose --profile stack-ingress up -d caddy`
  (same form for `registry`), or add them to `COMPOSE_PROFILES`. The 22 that come
  up pull about 9 GB of images (15 images, 9.0 GB on disk, measured on a clean
  install). The biggest are `api` ≈ 5.3 GB, `mod-worker` ≈ 840 MB,
  `keycloak/keycloak:26.3` ≈ 710 MB, `postgres:17.11` ≈ 650 MB, `postgres-app` ≈ 640 MB,
  the builder's upstream `moby/buildkit` ≈ 380 MB, `openbao/openbao:2.6.4` ≈ 290 MB,
  `workflow-builder` ≈ 290 MB and `mcp-gateway` ≈ 260 MB. The rest are small:
  `mod-dashboard`, `mod-docs`, `ghcr.io/modtim/valkey-enc` and
  `ghcr.io/modtim/versitygw-enc` are about 90-115 MB each,
  `axllent/mailpit:v1.33.1` ≈ 53 MB and the builder's `buildkit-fw` ≈ 16 MB.
  (`valkey-enc` and `versitygw-enc` are **our own** gocryptfs-wrapped images, not the
  upstream `valkey/valkey:8-alpine` or `ghcr.io/versity/versitygw:v1.7.0`, which the CE
  compose never references.) These are the sizes the images take on disk; round up
  generously for the build contexts
  (`docker-compose.dev-build.yml`), the named volumes and the daemon's layer
  cache. That is the install minimum; plan for more free disk over time: container logs, the audit log, image layers from each upgrade and your own data all grow, and ce-doctor warns when free disk is low.
  For the `registry` profile, the installer (`scripts/first-run-secrets.sh`) generates `REGISTRY_PASSWORD` into `.env` with the other credentials; `REGISTRY_USER` defaults to `mod` (set it in `.env` to change it). Then run `docker compose --profile registry up -d registry`. It listens on port `5000`
on **every** interface (`REGISTRY_BIND`, default `0.0.0.0`) so other worker machines can
pull built nodes; set `REGISTRY_BIND=127.0.0.1` to keep it on this machine. See `docs/node-builder.md`.
- **Ports that must be free** (reachable from your local network by default, because the installer detects this computer's address and prints it; add `--no-lan` to bind them to `127.0.0.1` only): `3000` Dashboard,
  `3001` WorkflowBuilder, `3002` docs site (`mod-docs`), `8000` API, `8080` Keycloak,
  `7070` versitygw, `5432` Postgres, `6379` Valkey, `8200` OpenBao, `8025` Mailpit web inbox
  (SMTP itself is internal-only: 1025 on `modtex-network`, never published).
  Check with `ss -ltn`.
- **Building (only if you build the CE images from source, or build your own
  nodes).** Some distro Docker packages (Ubuntu's `docker.io`) ship no `docker
  buildx` CLI. `docker compose build` and `docker compose ... up --build` still
  work — BuildKit runs inside the daemon — but plain `docker build` prints a
  deprecation notice and falls back to the legacy builder, which handles the
  `ADD https://…` download steps in node Dockerfiles less reliably. Prefer the
  compose forms; if you do use plain `docker build`, run one build at a time
  (many concurrent builds on one box make those downloads flake).

## Quickstart

```bash
git clone https://github.com/modtim/MOD_CORE_CE.git
cd MOD_CORE_CE
./scripts/install.sh
```

The installer creates your passwords, downloads the images and starts MOD. It
asks one question: **"Open this install to other computers on your network?
[Y/n]"**. Press Enter to accept the default: MOD opens to your network at the
address the installer detects (it prints it once). Answer `n` to keep MOD on
this computer only. To skip the question:
`./scripts/install.sh --lan` (open to the network, auto-detected address),
`./scripts/install.sh --host mod.office.lan` (open, use this name or IP), or
`./scripts/install.sh --no-lan` (this computer only). Without a terminal
(scripts, CI) and without a flag, and with `--yes`, the installer takes the default
and opens MOD to your network at the detected address (if it finds no address it
stays on this computer with a warning); add `--no-lan` to keep MOD on this computer only. Add `--no-pull` to skip the image download and use the images already on this computer (offline installs). `--bind ADDR` (with `--lan`: the address the published ports listen on, default `0.0.0.0`; ignored without `--lan`) chooses that address, and `--no-start` (write `.env` only, do not start MOD) lets you review the settings first. See "Accessing CE from another machine" below.

Prefer to run the steps yourself? The installer is a thin wrapper around them:

```bash
./scripts/first-run-secrets.sh    # run this first — generates random infra creds into .env
./keycloak/ensure-client-secrets.sh   # generates random Keycloak client secrets into .env
docker compose pull              # fetch the CE images + upstream images
grep -q '^COMPOSE_PROFILES=' .env || printf 'COMPOSE_PROFILES=builder\n' >> .env   # node builder on by default (install.sh does the same)
sudo bash scripts/install-buildkit-apparmor.sh   # node builder: AppArmor profile (safe to repeat; a no-op on hosts without AppArmor)
sudo bash scripts/first-run-sidecar.sh   # encrypted stores: creates the host cipher dirs + the 0600 passphrase file; skipping this makes Docker create an empty directory where the passphrase file must be
docker compose up -d
```

Opt-in profiles you enabled with --profile are not in .env: repeat the same --profile flags on pull and on up -d, or list them in COMPOSE_PROFILES in .env.

The node builder needs its AppArmor profile on AppArmor hosts (`sudo bash scripts/install-buildkit-apparmor.sh`, safe to repeat). If you cannot install it (no sudo), remove that `COMPOSE_PROFILES=builder` line from `.env` before `docker compose up -d`: continuing without the node builder. To enable it later run (safe to repeat):

```bash
sudo bash scripts/install-buildkit-apparmor.sh && scripts/install.sh --enable-builder && docker compose --profile builder up -d buildkitd-net buildkitd
```

`scripts/install.sh --enable-builder` only adds `builder` to the one `COMPOSE_PROFILES` line of `.env` (your other entries, such as `registry`, are kept, and running it twice changes nothing). Do not append a second `COMPOSE_PROFILES=` line by hand: Compose reads the last one and your list would be lost. Without the installer, the same merge as one line:

```bash
awk 'BEGIN{d=0} /^COMPOSE_PROFILES=/ && !d {d=1; v=substr($0,18); q=""; if (v ~ /^[ ]*["\047].*["\047][ ]*$/) {gsub(/^[ ]+|[ ]+$/,"",v); q=substr(v,1,1); v=substr(v,2,length(v)-2)} gsub(/^[ ]+|[ ]+$/,"",v); n=split(v,a,","); h=0; for(i=1;i<=n;i++){gsub(/^[ ]+|[ ]+$/,"",a[i]); if(a[i]=="builder")h=1} if(h){print; next} sub(/,+$/,"",v); print "COMPOSE_PROFILES=" q (v==""?"builder":v",builder") q; next} {print} END{if(!d) print "COMPOSE_PROFILES=builder"}' .env > .env.new && chmod 600 .env.new && mv .env.new .env
```

**First install: one `sudo` step.** The encrypted-storage folders live under
root-owned host paths, so on a first install `scripts/install.sh` can stop with
exit code 1 after printing "these host paths need root. Run exactly this one line"
followed by one line of the shape `sudo bash /path/to/MOD_CORE_CE/scripts/first-run-sidecar.sh`
and "STOPPED before starting". Nothing has been started and nothing is lost: run
that printed line, then run `./scripts/install.sh --yes` again (safe to repeat; your
passwords are kept).

**How long it takes.** Usually 10-20 minutes on a first run, longer on a slow
connection: the images are about 9 GB (about 10 minutes for the pull and first start
together was measured on a fast link; a 43-minute pull was measured on a slow one). The first
start downloads several GB of images; do not re-run the installer while it works.

**What to expect.** The first `docker compose pull` fetches about 9 GB and can take
tens of minutes on a slow link (about 43 minutes was measured on one clean-room
install). The first `docker compose up -d` took **12 m 18 s** from start to every
service healthy, measured on a clean machine (a 30-core Ubuntu 24.04 box) with the
CE images built from source via the `dev-build` override rather than pulled — the
upstream images (postgres, keycloak, openbao, valkey, versitygw, mailpit) were pulled
separately and took only a few seconds on that LAN, and the Docker base-image
cache was warm for `python:3.11-slim`. The bulk of it is the one-shot `mod-migrate` service applying the database
migrations on a fresh database; that takes several minutes on first start. An
upgrade replays only the migration steps the database has not recorded yet.
The API turns healthy about a minute after that. The API healthcheck allows a
600 s start period, so be patient rather than re-running; `docker compose logs -f` shows progress. Once it is up, open the URLs listed under
"Ports" below (they listen on the address the installer chose: your network by default, `127.0.0.1` with `--no-lan`) (API `:8000`, Dashboard `:3000`,
WorkflowBuilder `:3001`, Keycloak `:8080`).

> **Re-running is safe:** both scripts only fill in missing keys — every
> existing value is kept byte-identical. After an upgrade, run
> `scripts/first-run-secrets.sh` again before `docker compose up` to add
> any new keys. A key the compose file requires but `.env` lacks makes `up`
> refuse to start with `run scripts/first-run-secrets.sh`.

> **Upgrading an existing install?** The `postgres-app` image is PostgreSQL 17 (with the
> `pg_tle` extension). A database volume created by the earlier `postgres:17.11` image
> opens as is. If your volume was initialised by a different PostgreSQL major (e.g. 13),
> the new image cannot open it and `docker compose up -d` would take the database down.
> After `docker compose pull` and **before** `docker compose up -d`, run
> `scripts/pg-version-guard.sh -f docker-compose.yml` (read-only; it changes no volume or
> container; it passes on a fresh install and on a volume whose PostgreSQL major equals the
> major of the image you just pulled, which is PG17 for `postgres-app`; to see the image's
> major run `docker run --rm --entrypoint postgres <postgres-app image> --version`). If it refuses, nothing was changed; follow
> [docs/postgres-major-version.md](./postgres-major-version.md) (dump first, then restore).
> `pg_tle` is only CREATEd when the database is first initialised; an existing data
> directory keeps working but does not get the extension until you run
> `CREATE EXTENSION pg_tle;` once (the server already preloads it).

> **Re-running an existing stack?** If you started the stack before generating
> the Keycloak client secrets, the imported realm is stale — see
> "Resetting Keycloak (re-runs only)" below. **This step DELETES ALL KEYCLOAK
> USERS**; it is not part of a first bring-up.

### Choosing images

The CE-authored images are pulled, not built, by default. Their refs are
composed in the compose file from two variables (`${CE_IMAGE_REGISTRY:-ghcr.io/modtim}/<name>:${CE_IMAGE_TAG:-latest}`,
the `image:` key of the `postgres-app`, `redis`, `versitygw`, `api`, `mod-worker`, `mod-dashboard`, `mod-docs` and `workflow-builder` services; the first three use the images `postgres-app`, `valkey-enc` and `versitygw-enc`):

- `CE_IMAGE_REGISTRY` — default `ghcr.io/modtim`; set it to an alternate mirror
  such as `registry.example.com/my-project`.
- `CE_IMAGE_TAG` — the release tag this compose shipped with. A CE release is
  built from a git tag and the release build stamps that tag into this
  compose's `${CE_IMAGE_TAG:-<tag>}` default, so the images you pull are the
  ones cut from that release tag — not whatever `latest` happens to point at today.
  (The compose in the source repo keeps `latest` as the dev default; stamping
  happens at release build.) Override it to pull a different release's images.
- `CE_ENC_IMAGE_REGISTRY` — optional; the registry for the two encrypted-store
  images (`valkey-enc`, `versitygw-enc`) only. Defaults to `CE_IMAGE_REGISTRY`.
- `CE_ENC_IMAGE_TAG` — optional; the tag for those same two enc images.
  Defaults to `CE_IMAGE_TAG`.

`scripts/first-run-secrets.sh` appends any missing keys to `.env` on every run
and never overwrites existing values, so these two settings do **not** come from
that script — set them in the `.env` the script already created. If `.env` already
has a `CE_IMAGE_TAG=` line, edit that line; do not add a second one. If it has none,
add it once (a release tag such as `v1.0.0`, or `head-<12 hex>`, whichever the release notes name):

```bash
echo 'CE_IMAGE_REGISTRY=registry.example.com/my-project' >> .env
grep -q '^CE_IMAGE_TAG=' .env || echo 'CE_IMAGE_TAG=v1.0.0' >> .env
grep -c '^CE_IMAGE_TAG=' .env   # must print 1
# private mirror only: log in first (reads the token from stdin, never argv)
printf '%s' "$REGISTRY_TOKEN" | docker login registry.example.com -u <user> --password-stdin
docker compose pull              # pull with the new refs BEFORE starting
docker compose up -d
```

Opt-in profiles you enabled with --profile are not in .env: repeat the same --profile flags on pull and on up -d, or list them in COMPOSE_PROFILES in .env.

Skip the `docker login` line for a public registry. The CE images are public on
ghcr (`CE_IMAGE_REGISTRY` defaults to `ghcr.io/modtim`), so the default install
needs **no** `docker login`. You only need one for a private mirror, or if a pull
returns `denied` — then run `docker login ghcr.io` with a GitHub token that has
the `read:packages` scope (pipe it in with `--password-stdin`, as above). Only the default-profile CE-authored images plus the `builder` profile's `buildkit-fw` use CE_IMAGE_REGISTRY/CE_IMAGE_TAG; the upstream images (postgres:17.11, keycloak 26.3, openbao 2.6.4, mailpit v1.33.1) keep their own refs, plus, on the optional profiles, moby/buildkit:v0.34.2-rootless (builder), caddy:2-alpine (stack-ingress) and registry:2.8.3 (registry). To build the images
from source instead of pulling them, run
`docker compose -f docker-compose.yml -f docker-compose.dev-build.yml up --build`.

> **Do not add `--pull never` to a bring-up.** `docker compose up` pulls any
> missing image as part of starting (that is how a first run gets the upstream
> images). `--pull never` makes `up` fail when an image is not already local, so
> it is only correct once **every** image the compose references exists on the
> machine — including a `dev-build` override's, whose services still name the
> `ghcr.io/modtim/…` refs even though you built them locally under those exact
> tags. If you only want to skip building, use `docker compose up -d --no-build`
> (pull what is missing, build nothing). The `--pull never` you may see inside
> `scripts/pg-version-guard.sh` is internal to that read-only probe and is not a
> bring-up flag.

**Images beyond the compose set (first run).** The compose ships the CE images plus
the upstream infra images. A workflow run also needs a **node** image, and a
node build needs `build-task` + `containerizer` plus the `base-*` base image the
generated Dockerfile builds `FROM`. Those are published under the same
`CE_IMAGE_REGISTRY` namespace but are **not** compose services, so `docker
compose pull` never fetches them — the free node pack images are **pulled** from
the same namespace: `docker pull ghcr.io/modtim/build-task:latest` and
`docker pull ghcr.io/modtim/containerizer:latest`, then whatever `base-*` image
the generated Dockerfile builds `FROM`. The `dev-build` override covers the nine
compose services only, not the node pack. You can still `docker build` a node
image one at a time under the ref the worker asks for, and the worker then uses
them from the local store (`worker.prefer_local_images` — the
platform setting's default is **false**; until you set it, the worker's own
`MOD_WORKER_PREFER_LOCAL_IMAGES` env applies; CE's compose sets that to `1` on `mod-worker`, so the embedded worker prefers local images unless you override it). Build them **before** your
first run: an unbuilt node fails with
`pull image: … denied` in the worker log, and the worker retries rather than
crashing, so the fix is a local build, not a login. See
[node-builder.md](./node-builder.md) for what the wizard pulls and how big
each image is.

See [`docs/`](../docs) for configuration, node authoring, and operations guides.

### Resetting Keycloak (re-runs only)

Not a first-run step. Do this only when you already started the stack before
running `./keycloak/ensure-client-secrets.sh`, so the imported realm carries
placeholder client secrets. **It DELETES ALL KEYCLOAK USERS** — the accounts,
roles and grants you created are gone, and the next `up` re-imports the seeded
realm from scratch:

```bash
docker compose rm -sfv keycloak keycloak-db
docker volume rm mod-core-ce_keycloak_db_data
```

The compose file pins the project name to `mod-core-ce` (top-level `name:`),
so the DB volume is always `mod-core-ce_keycloak_db_data` regardless of the
clone directory name; confirm with `docker volume ls | grep keycloak_db_data`.
Then `docker compose up -d` again. `scripts/install.sh` notices this reset
(the Keycloak container got a new start time) and re-runs the `keycloak-admin-init`
one-shot itself, so the fresh Keycloak gets its permanent admin again without a
manual step; a plain `docker compose up -d` does not restart that one-shot, so
after a reset run `scripts/install.sh` (or `docker compose up -d --force-recreate
keycloak-admin-init`).
