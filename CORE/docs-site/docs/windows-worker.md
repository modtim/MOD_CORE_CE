---
sidebar_position: 5.5
title: Windows Workers
description: Running MOD on a Windows machine — the WSL2 path today, and the planned native (no-container) path.
dashboard_route: '#system'
---

# Windows Workers

MOD runs on a Windows machine in two ways. One works today; the other is planned.

## Today: WSL2 (community supported)

The path that runs today is **WSL2 + Docker Desktop**: install WSL2 and Docker Desktop, then run the
worker setup script inside your WSL terminal (see [Worker Setup](/worker-setup)). This path runs the
same container-based workers as Linux, with GPU passthrough through the `/dev/dxg` bridge. NVIDIA is
working (verified on an RTX 5000 Ada under WSL2); Intel Battlemage and AMD R9700 drivers
crash on compute dispatch through WDDM.

## Planned: the native Windows path (no containers)

MOD also has a **native** Windows worker — a single Go binary that runs tasks as host processes, with
no container runtime. Its code is in development and has not completed a live end-to-end
release yet, so treat it as **planned**, not something you can set up today.

A native Windows installer and a desktop tray app are not part of this edition. Neither is in its
source tree, so nothing here installs them.

- **Native installer (not in this edition)** — there is no MSI installer for the native path in this
  edition. The worker binary itself is the only Windows piece that ships.
- **Tray icon** — the worker binary's tray icon is suppressed while the worker runs as a Windows
  service, so a service-installed worker shows no tray icon. The supported Windows path is the
  WSL2 + Docker Desktop worker described above.
- **Single-tenant enrolment** — a native worker is enrolled to exactly one tenant, and the platform
  never routes platform-pool work to it.

## What is already in place for the native path

- The worker binary cross-builds for `windows/amd64` and carries a Windows service mode.
- A PowerShell install script (`worker_install.ps1`, served by the platform's worker-provisioning
  endpoints) registers the `MODWorker` service and its restart schedule; the Dashboard's Add Worker
  page shows a Windows one-liner for it.
- The worker's local management UI (loopback-only, per-boot token) is built.

Get the command from **Dashboard > Workers > Add Worker > the Windows or macOS tab** (valid 15 minutes, one use).
- Code signing for Windows artifacts is not yet wired into any released artifact in this edition. Nothing in this edition signs
  Windows code today, so Windows may show an unknown-publisher warning.
