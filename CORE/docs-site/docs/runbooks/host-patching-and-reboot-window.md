---
sidebar_position: 58
slug: /runbooks/host-patching-and-reboot-window
title: Patch the host OS and reboot the box
description: "Runbook: an operator of a self-hosted Community Edition install applies OS, kernel and Docker-engine security updates to the host that runs their install, decides whether the change needs a reboot, orders that reboot against the containers (drain first, host action, recover, undrain), and verifies the workers and persistent stacks came back."
---

# Patch the host OS and reboot the box

Last reviewed: 2026-11-16

Your install runs on a Linux host you own. Security updates at that layer —
the kernel, the Docker engine package, the distro libraries under the
containers — are a **different change from a MOD release**: a release swaps
images and code, a host update changes what runs *underneath* the containers
and does not touch the containers themselves. The one thing both layers share
is the reboot: rebooting the host restarts the machine, not the containers,
and a container that was running before the reboot is not automatically
running after it.

This page is the host layer only. For the API container restart itself use
[Restart the API without losing runs](/runbooks/api-restart-and-drain); for
the state an install keeps on disk use [Back up an install and restore it
onto a new box](/runbooks/ce-backup-and-restore).

## 1. What to patch, and how often

| What changed | Effect on your install | Reboot |
|---|---|---|
| Ordinary apt packages, distro libraries | userland only | no window needed |
| Docker engine package | daemon and its userspace | yes — treat as a window |
| Kernel (`linux-image-*`) | the ABI under every GPU stack | yes, and only inside a window |
| GPU userspace libraries | host/container boundary | yes — treat as a window |

Enable `unattended-upgrades` per host so the **package fetch** runs on its own
schedule; the reboot is the part that needs a window, not the fetch.

```bash
sudo apt-get install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades   # enables the security class
```

Three policy lines matter on a MOD host (`/etc/apt/apt.conf.d/`):

- `Unattended-Upgrade::Remove-Unused-Dependencies "false";` — the install's
  provisioning script (`CORE/API/worker_setup.sh`) installs Docker and GPU
  packages that nothing else declares a dependency on. Auto-removal can pull
  a library a live worker needs.
- `Unattended-Upgrade::Automatic-Reboot "false";` — leave the reboot to §2.
  An unattended reboot of a MOD host strands containers (§3).
- `Unattended-Upgrade::Release-Package-Kernel "true";` only on hosts with no
  persistent GPU stack pinned to a kernel ABI. Otherwise **hold** the kernel
  package and schedule it inside a reboot window you control.

After a distro upgrade, re-run `CORE/API/worker_setup.sh` rather than
hand-editing apt sources — it already guards package-manager health.

## 2. The reboot window, in order

Order is fixed: **drain first, then act, then recover, then verify.** Never
check before you act, and never undrain before you verify.

1. **Stop dispatch.** Set the `dispatch.paused` platform setting (the
   Platform Settings tab of the Dashboard admin UI, not an env var;
   `POST /v1/system/drain` does the same thing and reports in-flight counts).
   No new workflow runs or task claims start; workers finish what they already
   hold.
2. **Wait for in-flight work to clear.** Poll `GET /v1/system/drain-status`
   until `in_flight_runs` and `in_flight_tasks` are both `0`. Capture the
   counts at pause time — that is the number you wait on. Do not proceed on a
   "looks idle" guess, and do not re-check after you start rebooting.
3. **Snapshot what you cannot rebuild.** If the reboot is also the moment you
   replace the box, take the pieces
   [Back up an install and restore it onto a new box](/runbooks/ce-backup-and-restore)
   names before anything is recreated. A plain OS update inside a window does
   not need them; a hardware change does.
4. **Take the host action.** `apt-get upgrade`, then reboot **only** if the
   upgrade touched the kernel, the Docker engine, or a GPU userspace library.
   Schedule the reboot in the low-traffic slot for your tenants.
5. **Keep the API down short.** Target under ~120 s of API outage. A worker
   that cannot reach the manager past its heartbeat lease **self-fences and
   stops the external GPU stacks it hosts** — correct behaviour, but every
   stack then pays a stop-and-wake cycle and the restart takes much longer.
6. **Let the boot recovery run, or run it yourself** (§3).
7. **Undrain.** `POST /v1/system/resume` clears `dispatch.paused`.
8. **Re-deploy workers and stacks** only after §4 verification passes.

## 3. The boot recovery, and how to get it back by hand

Docker restart policies do not fire when the container start itself fails, so
a reboot can leave a service stranded until someone acts. Two causes are
common on a patched host: a published port bound to a LAN address that
Wi-Fi/DHCP assigns *after* the daemon restores containers (the start fails with
`cannot assign requested address` and is never retried), and the one-shot that
seals the secret store on first run coming back **sealed**.

Community Edition ships the recovery for both, at
`scripts/recover-after-reboot.sh`. It is idempotent — a healthy stack is a
no-op that prints `nothing to do` — and it never rotates secrets. Run it
yourself after a reboot:

```bash
sudo bash scripts/recover-after-reboot.sh
```

To see what it would do without doing it:

```bash
scripts/recover-after-reboot.sh --dry-run -f docker-compose.yml
```

What it does, in order: waits for the docker daemon; re-attaches any container
left with no network endpoint; starts the exited infra (the secret store, the
databases, the cache) and waits for health; re-runs the store's one-shot
**only** if it reports Initialized+Sealed; starts the remaining services in
dependency order, retrying a failed start while the LAN address is still
coming up; then waits for health and prints a summary table. Exit `0` means
every managed container is running and healthy (or there was nothing to do);
exit `1` means something is still unhealthy or sealed.

To have the boot do it for you, install the shipped unit
(`scripts/mod-ce-recover.service`) — it runs that same script once after the
daemon starts:

```bash
sudo install -m 644 scripts/mod-ce-recover.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable mod-ce-recover.service
```

Edit the unit's user, working directory and compose project name first (the
three paths at the top of the file). Check it with
`systemctl status mod-ce-recover.service` and read the run with
`journalctl -u mod-ce-recover.service`.

## 4. Verify the fleet came back

Sweep, then check each item — do not assume:

```bash
scripts/ce-doctor.sh                            # every service should read OK
```

- [ ] **The secret store came back unsealed.** A reboot can leave it sealed:
      the store's one-shot is a one-shot (`restart: "no"`), so nothing
      unseals it by itself. `scripts/recover-after-reboot.sh` re-runs that
      one-shot only when the store reports Initialized+Sealed, and it never
      rotates anything. Before you call the window closed, probe both halves
      of the store's state: that it is **unsealed**, and that its storage
      **backend** still answers — the CE store is a single-node `raft`
      installation whose state lives in the `openbao-data` volume at
      `/openbao/data/raft`. A store that is up but sealed, or up but with an
      unreadable raft dir, is a failed window even when every container reads
      healthy.
- [ ] Every persistent stack is back to **running** and re-claimed by the
      worker that owns it (see
      [Deploy and recover a persistent stack](/runbooks/stack-deploy-and-recover)
      for the stuck-deploy failure modes).
- [ ] Each worker reports its current fence lease, and no fence event fired
      during the window.
- [ ] `GET /v1/health` answers `ok`, and `dispatch.paused` is back to `false`.
- [ ] One real workflow run, end to end, before you call the window closed.
- [ ] If the window also carried a MOD release, record it the way
      [Upgrade and Rollback](/security/upgrade-and-rollback) describes.

What you should see on a CE install that carries the recovery unit across a
real reboot — one counted run, not a promise: 15/15 containers Up and healthy
2 minutes after the reboot, the recovery unit reporting success (exit `0`),
and 0 manual steps needed.

## Why the order matters

- **Drain before you reboot.** A task left mid-flight when the API dies has to
  be re-driven; finishing in-flight work first is what keeps runs intact.
- **The 120 s fence is not an error.** It is the worker protecting its own
  GPUs. Fighting it (restarting the API twice, rebooting with the API already
  down) makes the window longer, not shorter.
- **Recover before you undrain.** Undraining onto a stranded fleet dispatches
  work to workers that cannot execute it.
- **Verify the store before you close the window.** A sealed store leaves every
  later secret read failing long after the reboot looked fine.

## Related

- [Restart the API without losing runs](/runbooks/api-restart-and-drain) — the
  container-level restart inside this window, and the fence lease in detail.
- [Deploy and recover a persistent stack](/runbooks/stack-deploy-and-recover) —
  waking a stack a reboot fenced.
- [Enrol a worker, or retire one](/runbooks/worker-enrol-and-retire) — if the
  patch window is also when a worker changes hosts.
- [Back up an install and restore it onto a new box](/runbooks/ce-backup-and-restore)
  — what the install keeps on disk, and where the store's raft snapshot goes.
- [Monthly patch release](/runbooks/monthly-patch-release) — the release layer
  on the same host, and how the two layers differ.
