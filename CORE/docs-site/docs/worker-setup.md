---
sidebar_position: 5
title: Worker Setup
description: Machines that run processing tasks. Embedded workers (in Compose) vs remote workers. Provisioning, scaling, monitoring.
dashboard_route: '#system'
---

# Worker Setup

Workers are the machines that run your processing tasks. MOD supports embedded
workers (in the Docker Compose stack) and remote workers.

In Core Edition (CE) the embedded worker `mod-worker-embedded` in `docker-compose.yml`
(Compose service `mod-worker`, container `my_mod_worker`) is the default: it is part of the stack
and needs no enrollment. Everything below is for running an extra worker on a
machine outside the stack.

No GPU is needed: the built-in worker runs CPU nodes. Add a GPU machine below when you want GPU nodes.

## Supported Platforms

| Platform | Status | Notes |
|----------|--------|-------|
| **Linux** (Ubuntu, Debian, RHEL, Fedora) | **Fully supported** | Recommended for production. GPU drivers auto-installed. |
| **Windows** (via WSL2) | Community supported | Install [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install) + [Docker Desktop](https://www.docker.com/products/docker-desktop/), then run the setup script inside your WSL terminal. Not officially tested — may need manual troubleshooting. GPU passthrough requires NVIDIA drivers installed on the Windows host. A **native** Windows path (signed installer + desktop tray app, no containers) is **planned** — see [Windows Workers](/windows-worker). |
| **macOS** | Development only | A native worker path exists — install script (`worker_install_macos.sh`), launchd service, and Metal GPU queue for native tasks only. See [macOS Workers](/macos-worker) for details. Apple does not license macOS for server use. No GPU passthrough to containers. |

For production workers, **use Linux**. The setup script handles everything —
Docker, GPU drivers (NVIDIA, AMD, Intel), the worker binary, and enrollment.

## Adding a Remote Worker (CE)

CE serves the setup script and the worker binary from your own API. The target
machine only needs network access to your install (plus its normal OS package
repositories for Docker and GPU drivers).

Your install must be reachable from that machine: run `./scripts/install.sh --lan` first (see [Getting Started](/getting-started)).

**Before you start**

- The second machine must reach the API on port 8000 (published by the install; `--lan` makes it reachable from the LAN).
- The registry port 5000 is needed only if you pull built nodes (the `registry` profile).

1. In the Dashboard open **Workers → Add Worker**, fill in the form and click
   **Generate Setup Command** (platform admin). This creates an enrollment token
   (`POST /v1/workers/enrollment-tokens`) and prints a ready-to-paste command.
2. On the target machine, paste it. For Linux it looks like:

```bash
curl -fsSL "https://your-api/v1/workers/setup-script" | sudo ENROLLMENT_TOKEN='<token>' bash
```

   Replace `https://your-api` with the URL your install prints (plain `http` on a default LAN install). The command already carries your API URL (the served script fills it in) and the
   token. The worker name defaults to `worker-<hostname>` (or `worker-<machine-id>`
   when the hostname is a generic one such as `localhost`); a script run from a
   terminal asks you to confirm it, while a piped run (the command above) uses the
   default. To choose a name, add `WORKER_NAME='<name>'` next to the token, or append
   `--worker-name <name>` after `bash -s --`. The script validates the
   token (`POST /v1/workers/enroll/validate-token`), installs Docker and GPU
   drivers, downloads the worker binary from your API
   (`GET /v1/workers/download/{os}/{arch}`), enrolls
   (`POST /v1/workers/enroll/start`) and installs a systemd service. The worker
   enrolls as a normal remote worker; the names `mod-worker-embedded` and
   `mod-worker-ce` belong to the built-in worker in the Compose stack and the
   script refuses them.

The Dashboard also offers **Windows** and **macOS** tabs. They use a short-lived,
single-use download token (`POST /v1/workers/agent/download-token`) and the
installers served at `GET /v1/workers/agent/install-script`.

Run every `docker compose` command below from the checkout root (the directory with `docker-compose.yml` and `.env`).

The worker binary comes from your own stack, not from the internet: the built-in
worker `mod-worker-embedded` (Compose service `mod-worker`, container `my_mod_worker`) publishes every platform build (linux amd64/arm64,
windows amd64, macOS amd64/arm64) into the `worker-releases` bucket each time it
starts, and the API serves them from there. If a download answers `404` right
after a fresh install, wait for the `my_mod_worker` container (embedded worker `mod-worker-embedded`) to become healthy (or
run `docker compose restart mod-worker`) and retry — the log line
`release published` confirms the upload. If the download still fails and you
run the script from a terminal (not piped), it pauses so you can copy a worker
binary to `/opt/mod-worker/mod-worker` yourself and press Enter.

The script will:
- Install Docker if not present
- Detect and install GPU drivers (NVIDIA, AMD, Intel)
- Download the worker binary from your API
- Enroll with the platform
- Set up a systemd service

### Verify & manage the worker

Once the script finishes, the worker runs as a systemd service. Check it from the
host:

```bash
systemctl status mod-worker        # is it running?
journalctl -u mod-worker -f        # live logs (enrollment, heartbeats, task pickup)
systemctl restart mod-worker       # restart after a config change
```

It should appear as **Active** in **Dashboard → Workers** within a few
seconds of its first heartbeat. A new worker waits as **Pending** until you click **Approve** on its row in **Dashboard > Workers** (or tick **Auto-approve** when you generate the command). An expired enrolment token is refused at setup: generate a new command.
Enrollment status is also readable directly: `GET /v1/workers/enroll/status/{worker_name}`.

## Hardware Tags

Workers auto-detect their GPU type (CUDA, ROCm, Intel). You can add custom tags
during setup or via environment variable:

```bash
WORKER_HARDWARE_TAGS=tensorrt,triton
```

These tags are used by the scheduler to select the right container variant for each task.

## Hardware Benchmarking

New workers are automatically benchmarked when they first connect. The benchmark
measures:
- CPU (multi-threaded + single-threaded)
- Memory bandwidth
- Disk I/O (sequential + random read/write)
- GPU compute
- Object storage throughput

Results are stored in the worker's Hardware Profile and used by the ML scheduler
for optimal task placement.

## Monitoring

Workers appear in **Dashboard > Workers** with:
- Status (Active / Offline / Pending)
- Hardware specs (CPU, RAM, GPU model, VRAM)
- Benchmark status (Profiled / Profiling / Not profiled)
- Current activity (idle / running tasks)

## Tuning concurrency

A worker runs as many tasks at once as its limits allow. Both limits default to
**0 = unlimited**, and you can set either one:

- **Fleet-wide:** the "How many tests each worker runs in parallel" slider in the
  Training section of Settings (0 to 16, where 0 = unlimited). It sets the
  server-side limit for every enrolled worker and reaches each worker on its next
  heartbeat (about 15s by default).
- **Per host:** `max_concurrent_jobs` in the worker's config file, or the
  `MAX_CONCURRENT_JOBS` environment variable. This is read at start-up, so run
  `systemctl restart mod-worker` after changing it.

When both limits are set, the worker uses the lower of the two.

CE scales by the compute you run yourself. There is no limit on the number of
workers, and a Kubernetes cluster (or several) you own can run tasks and stacks.
