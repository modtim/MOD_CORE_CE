---
sidebar_position: 5.6
title: macOS Workers
description: Running MOD on a Mac — the native worker path today, what works, and what is still planned.
dashboard_route: '#system'
---

# macOS Workers

MOD runs on a Mac via a **native** worker — a single Go binary that executes tasks as host processes, with no container runtime. Apple does not license macOS for server use, so native workers on Mac are intended for development, testing, and light workloads only.

## Today: what works on macOS

MOD's native macOS worker can do real work today through the following paths.

### Install and run

The **install script** (`worker_install_macos.sh`, served from the platform's worker-provisioning endpoints) does everything needed to get the worker on a Mac:

- Downloads the correct `darwin` binary.
- Writes a **launchd** service plist (`com.modtechlabs.modworker`), registers it with `launchctl`, and starts it.
- Handles `--uninstall` cleanly.

Get the command from **Dashboard > Workers > Add Worker > the Windows or macOS tab** (valid 15 minutes, one use).

A manual launchd setup is also possible: drop the plist into `~/Library/LaunchAgents/` and run `launchctl load`. The script automates this.

### Metal queue — native GPU tasks

Macs with Apple Silicon (M-series) or an AMD GPU are eligible for the **Metal queue**. MOD's scheduler has a dedicated `METAL_QUEUE` that places native Metal tasks only on Mac workers. These tasks run directly on the GPU via Metal — no translation layer.

- The Metal queue uses its own claim-guard logic so that Mac GPU tasks never compete with CUDA/ROCm work.
- Metal queue claim guards live in the task-claim query and are enforced when a worker claims a task.
- **Native tasks only** — a Mac never claims container tasks. Container work stays on Linux/WSL workers.

### Unified memory scheduling

Apple Silicon uses unified memory (CPU + GPU share the same RAM pool). MOD's scheduler detects unified-memory workers and applies its overlap scheduler, timeline simulator, and contention model with a shared-pool memory budget so that Metal tasks respect the limited memory against other work on the same host.

### Local management UI

Every installed worker carries a loopback-only management UI (per-boot token auth). Open `http://localhost:<port>` in a browser to see worker health, Pause / Resume task claiming, and view logs.

### Calibration — native tasks bypass

When a new worker joins, the calibration/benchmark gate is automatically **skipped for native tasks on every native worker**, not only on macOS. A native task never runs in a container, so it has no container benchmark profile to wait on.

## What is not yet available

The following items are **planned** and not present in the current release. Do not expect them to work today.

- **Metal detection — full GPU probe** (planned) — the worker reads `hw.memsize` on darwin and reports VRAM from that unified pool (`gpu_darwin.go`). A real GPU probe (Metal device enumeration) is planned but not yet built. Without it, the worker cannot reliably advertise its GPU capabilities to the scheduler.
- **Metal telemetry — IOKit power APIs** (planned) — Darwin GPU utilisation and power draw are live: the monitor loop samples them on every beat (`internal/monitor/monitor.go` → `SampleGPU` → `gpu.go` "Metal" case → `gpu_darwin.go`, which runs `powermetrics --samplers gpu_power,tasks` and parses it in `internal/monitor/powermetrics_parse.go`). What remains planned: the IOKit power APIs (no IOKit call exists in the shipped worker code).
- **Code signing and notarization** (planned) — nothing in this edition signs the worker binary. No `codesign`, `notarytool`, or `sign-darwin.sh` exists. Future releases will sign the binary and ship through Apple's notarization pipeline.
- **.pkg installer** (planned) — the install script today uses `curl` + plist + `launchctl`. A proper `.pkg` built with `pkgbuild`/`productbuild` is planned for zero-config install.

## No stacks on macOS

A native macOS worker runs no containers and takes no stack work. Container nodes stay on the Linux/WSL path; native nodes (Metal GPU tasks) run on the native worker. MOD never places container work on a native worker.

## Known limitations

- **No container GPU passthrough** — Docker Desktop on macOS cannot pass Metal to containers. This is a Docker limitation, not an MOD one.
- **Apple Silicon memory pressure** — unified memory is shared between CPU and GPU. Under heavy Metal workloads the scheduler may throttle task parallelism to avoid OOM. This is handled automatically by the unified memory scheduler.
- **No official server license** — macOS is not licensed by Apple for server workloads. Native workers on Mac are not supported for production fleet use.

## What is already in place for the native path

- The worker binary cross-builds for `darwin/amd64` (Intel Mac) and `darwin/arm64` (Apple Silicon).
- The install script (`worker_install_macos.sh`) registers the `com.modtechlabs.modworker` launchd service, handles `--uninstall`, and is served by the worker-provisioning endpoints.
- The worker's local management UI (loopback-only, per-boot token) is built.
- The Metal queue and claim-guard logic are in place and tested in the scheduler.
- The unified memory scheduler, timeline simulator, and contention model all include darwin paths.
- Native task routing works: native tasks dispatch to the Metal queue.
