# Troubleshooting

Fixes for the problems first installs hit most often. [Back to the README](../README.md).


- **`permission denied` on `/var/run/docker.sock` (docker compose fails at once).**
  Cause: your user is not in the `docker` group. Fix: `sudo usermod -aG docker $USER`
  then `newgrp docker` (or log out), then re-run `scripts/install.sh` — it is
  idempotent and keeps your answers and passwords. Do NOT run the whole install
  with sudo: a sudo first run leaves `.env` owned by root (see Prerequisites).
- **`install.sh` says "the image download failed and these images are not on this computer".**
  Cause: no internet, broken DNS, or a registry you are not logged in to. The CE
  images are public on `ghcr.io/modtim`, so the default install needs no login.
  Fix: fix the network/registry and re-run `scripts/install.sh` (safe to repeat -
  your `.env` and passwords are kept); or, fully offline, build the images from
  source (`docker compose -f docker-compose.yml -f docker-compose.dev-build.yml up --build`)
  and run `scripts/install.sh --no-pull`. `install.sh` never starts MOD in this
  case, so nothing half-started needs cleaning up.
- **The `builder` profile's `buildkitd` container exits or stays unhealthy, and its log
  shows `permission denied` on a mount or an `apparmor="DENIED"` line.**
  Cause: the AppArmor profile `mod-buildkitd` is not loaded on this host, so the stock
  Docker profile blocks the mounts BuildKit needs. Check with
  `sudo aa-status | grep mod-buildkitd` (no output = not loaded) and
  `docker compose --profile builder logs buildkitd | tail -20`. Fix: from the extract root run
  `sudo ./scripts/install-buildkit-apparmor.sh`, then
  `docker compose --profile builder up -d`. On a host without AppArmor, put
  `CE_BUILDKIT_APPARMOR=unconfined` in `.env` instead and bring the builder up again.
- **`docker compose up` fails with `port is already allocated` / `address already in use`.**
  Cause: the services publish host ports on `127.0.0.1` — fixed for `5432`, `6379`,
  `8200` and `8025`, and bound to `${CE_BIND_ADDR:-127.0.0.1}` for `7070`, `8000`,
  `8080` (host side `KEYCLOAK_HOST_PORT`), `3000`, `3001` and `3002` (mod-docs); see the `ports:` lines in
  `docker-compose.yml` and a local Postgres, Valkey/Redis or dev server already
  owns one. Fix: stop that process, or change the host side (left of the colon)
  for a service nothing in your browser depends on (`5432`, `6379`, `8200`, `8025`).
  Keep `8000`, `8080`, `7070`, `3000`, `3001`, `3002` as shipped: the browser URLs and
  presigned links assume them. Then `docker compose up -d` again.
- **`docker compose up` fails with `Conflict. The container name "/my_..." is
  already in use` (or a network `mod_modtex-network` error).**
  Cause: the compose file uses fixed container names (`my_*`, `mod_*`, `workflow_builder`) and a fixed network
  name, so a second MOD/CE checkout or an older trial on the same host collides.
  Fix: `docker ps -a --filter name=my_ --filter name=mod_ --filter name=workflow_builder` to find them, then stop/remove the old
  trial from ITS checkout (`docker compose down` there). Run one CE stack per host.
- **A stack variable of kind `secret_file` fails to deploy (`file secrets cannot be
  deployed on this worker`).** The embedded `mod-worker` needs the same-path `/run/mod-secrets`
  bind shipped in `docker-compose.yml`; that needs a Linux host whose `/run` is tmpfs (systemd/WSL). Docker Desktop on macOS: unsupported for secret files.
- **After a host reboot some containers stay `Exited (255)` with no network, and/or
  the API/worker stay unhealthy because OpenBao reports `Sealed true`.** Fix:
  the `openbao-unseal` sidecar re-unseals automatically at start. If it does not,
  run `scripts/recover-after-reboot.sh` (add `-p <project>` and every `-f <compose-file>`
  you start the stack with; `--dry-run` first to see what it would do). It re-attaches
  containers that have no network endpoint (with their `container_name` + service aliases),
  starts exited services in dependency order and waits for health. Plain
  `docker compose up -d` also works but does not fix the missing network endpoints.
  If the API stays unhealthy: check `docker compose ps` — the `my_openbao_unseal`
  container must be `healthy` (healthcheck probes `bao status`; exit 0 means unsealed).
  If it is `unhealthy`, the sidecar loop is stuck or the key file is missing;
  `docker compose logs openbao-unseal` and `docker compose logs openbao`.
  If you bind published ports to a LAN address (`<lan-addr>:8000:80`)
  that DHCP/Wi-Fi assigns after dockerd has restored containers, those containers
  fail to start (`cannot assign requested address`), dockerd does not retry that
  start, and the failed start drops their network endpoint. Binding to `127.0.0.1`
  (the shipped default) avoids it; otherwise install `scripts/mod-ce-recover.service`
  (a ready systemd `oneshot` template ordered after `docker.service`) and, for the
  LAN-address case, the `docker.service.d` drop-in described in that template so
  dockerd waits for the address before it restores containers.
- **`openbao-init` exits 1: "already initialized but no local unseal key file".**
  Cause: only one of the `openbao-data` / `openbao-init-state` volumes was deleted
  (`scripts/first-run-openbao-init.sh` refuses to re-init over mismatched state).
  Fix (a local trial only, this erases tenant secrets): `docker compose rm -sfv
  openbao openbao-init && docker volume rm mod-core-ce_openbao-data
  mod-core-ce_openbao-init-state && docker compose up -d`.
- **Worker log shows `pull image: ... denied` for a node (an `image-ops`-style
  node container) while the CE images pulled fine.** Cause: that node's image
  is not published to the registry. Fix: build it from the tree, e.g.
  `docker build -t <the ref in the error> CORE/DockerContainers/<node>/`, and let
  the worker use local images (platform setting `worker.prefer_local_images`,
  `CORE/API/services/platform_settings.py`).
- **API log lines `rate guard FAIL-OPEN (unavailable)` at boot.** Expected
  in Community Edition (the rate-limit guard is not included in this build; requests are
  not blocked). No action.
- **The worker logs `pull image: … denied` for `mod/fixture-migrator` right at boot,**
  and repeats it every heartbeat. Cause: the platform calibration-fixture cutover
  (`fixture_shared_bucket_enabled`, default **off**, `CORE/API/services/platform_settings.py`)
  ships the shared fixture bucket and its image; until that release, the task has
  nothing to pull. No action, and **not** a login problem — do not `docker login`.
  Everything you build, the wizard and the free node pack all work without it.
- **Running the repo's browser test suite (`CORE/e2e/`) against CE.** That harness
  is a development tool: it is **not** in the built Community Edition tarball
  (deny-by-default manifest — `CORE/e2e/` is not shipped in the CE extract at all),
  so this bullet applies only to the private dev tree, where you run it against a
  CE-shaped stack. Its default targets are the dev-box names — fixed
  `my_*` container names, localhost URLs, one shared login. For CE it already
  has the right knobs: set `E2E_TARGET=ce` and point it at the stack with
  `CE_DASH_URL` / `CE_WB_URL` / `CE_KC_URL` / `CE_API_URL` (see the header
  comment of `CORE/e2e/run.sh`). It also refuses to start unless **all six**
  seeded principals exist (`e2e-admin`, `e2e-sub`, `e2e-onboard`,
  `e2e-platform`, `e2e-tenantb`, `e2e-tenantc` — `CORE/e2e/seed_principals.sh`);
  seeding three accounts aborts the whole suite, which is by design, not a CE
  failure. Token lifetime is the Keycloak realm default, so a long suite can
  need a fresh token mid-run — that is a harness/env matter on your test box,
  not something to change in CE.
- **Warnings at boot that change nothing:** `create_tables: bootstrap lock file
  /run/mod-db-bootstrap.lock unavailable (Permission denied); running the DDL
  unguarded` (the lock is a cross-process guard only; the DDL is idempotent), and
  many `append-only guards: … DuplicateObject / tuple concurrently updated` lines
  (several bootstrap workers racing on the same table; each statement retries).
  Both are informational — `docker compose up -d` still reaches healthy, and a
  re-run is a no-op. If the database itself fails, `up` says so and stops.
- **`start_period`/slow start, Keycloak login failing, restarts, low disk.** See
  Quickstart ("What to expect"), "Resetting Keycloak (re-runs only)" and
  Prerequisites (`scripts/ce-doctor.sh`, free disk) above.
