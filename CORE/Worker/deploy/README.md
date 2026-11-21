# MOD worker host configuration

Drop-in configs for hardening the Docker daemon underneath `mod-worker`.
Apply once per worker host.

## `daemon.json`

Merge into `/etc/docker/daemon.json` (or replace if the host has no existing
config) and run:

```bash
sudo systemctl restart docker
```

This sets:

- **`userns-remap: default`** — root-in-container maps to an unprivileged
  host UID. Container-escape CVEs need to chain a kernel bug on top of
  the container bug to actually be root on the host. Requires every
  bind-mounted host directory the containers need to write to be owned
  by the remapped UID range — no helper ships in CE: chown every bind-mounted host dir the containers write to yourself (`chown -R <remapped-uid>:<remapped-gid>`) before the first worker start.
- **`runtimes.runsc`** — registers gVisor as a secondary OCI runtime.
  Nodes opting into `use_sandbox_runtime=true` run under `runsc` for
  VM-grade isolation at ~2-3× perf cost. When `runsc` isn't installed,
  the worker silently falls back to the default runtime.

## Installing gVisor

```bash
curl -fsSL https://gvisor.dev/archive.key | \
    sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) \
    signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] \
    https://storage.googleapis.com/gvisor/releases release main" | \
    sudo tee /etc/apt/sources.list.d/gvisor.list > /dev/null
sudo apt-get update && sudo apt-get install -y runsc
```

Then tell `mod-worker` it's available:

```bash
systemctl edit mod-worker.service
# Add:
#   [Service]
#   Environment="GVISOR_AVAILABLE=true"
systemctl daemon-reload
systemctl restart mod-worker.service
```

`SelectRuntime()` in the worker's executor reads `GVISOR_AVAILABLE` and
only emits `runtime: runsc` in the container create call when it's `true`
AND the payload requested it — otherwise it lets dockerd use the default
runtime.

## Verifying

- `docker info | grep -A1 Runtimes` should list `runsc` alongside `runc`.
- `docker info | grep -i userns` should show `userns-remap: default`.
- A node run with `use_sandbox_runtime=true`: `docker inspect <id> |
  grep Runtime` → `"Runtime": "runsc"`.
- With userns active, `ps -ef` on the host shows the container process
  running under a high-numbered UID (`231072` or similar) even though
  `id` inside reports `uid=0`.
