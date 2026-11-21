---
sidebar_position: 44
slug: /security/docker-socket-proxy
title: Docker Socket Proxy
description: "For tasks and stack services that need a Docker socket: a policy proxy in front of the daemon that checks every call against an allowlist, and it is off by default."
---

# Docker Socket Proxy

## What it is

Some tasks legitimately need a Docker socket inside their container
(tasks that set `needs_docker_socket`, and stack
sibling containers managed through the daemon). Giving a tenant
container the *raw* `/var/run/docker.sock` is the same as handing it
root on the worker: from that socket a container can start a
privileged container, bind `/` into a new container, and read
anything.

The Docker socket proxy (`CORE/Worker/internal/sockproxy`) sits between
the container and the daemon. The container gets the proxy's own unix
socket bind-mounted at `/var/run/docker.sock`; every docker call it
makes flows through the proxy first, and the proxy checks the call
against an allowlist — including the request **body** of container
creates and execs — before letting it reach the real daemon.

This is **Docker-outside-of-Docker, not true Docker-in-Docker**: the
container can see and drive containers on the worker, but it cannot
give those containers privileges the worker itself does not have
(privileged mode, host namespaces, host filesystems). It cannot
install docker plugins, manage a swarm, read daemon secrets, or copy
files in/out of other containers.

## Modes

The mode comes from the platform setting `worker.docker_proxy_mode`,
delivered to every worker in the heartbeat tunables map (no restart,
applies within two heartbeats).

| Mode | Behaviour |
| --- | --- |
| `off` (default) | Inert. No proxy is started; containers get the raw socket exactly as before. |
| `report` | The proxy is in the path but forwards **everything**. Calls the policy would deny are logged (one structured line: method, path, mode, reason — never the request body) so you can see what real workloads need before enforcing. |
| `enforce` | Denied calls get a `403` with a JSON `{"message": reason}` and never reach the daemon. |

Roll out in order: `off` → `report` (read the logs for a day or two)
→ `enforce`. If `report` or `enforce` is armed but the proxy itself fails to start,
the worker mounts **no** docker socket at all and logs the failure —
it never silently falls back to the raw socket.

## What is allowed

**Read-only daemon introspection** (`GET`):
`_ping`, `version`, `info`, `events`, `containers/json`,
`containers/{id}/json`, `containers/{id}/logs`, `images/json`,
`images/{name}/json`, `networks`, `networks/{id}`, `volumes`,
`volumes/{name}`.

**Container lifecycle** (`POST`): `containers/create`,
`containers/{id}/start|stop|kill|wait|remove|attach`,
`containers/{id}/exec` (create), `exec/{id}/start`.

**Images, networks, volumes, builds** (`POST`): `images/create`
(pull), `build`, `networks/create`, `networks/{id}/connect`,
`networks/{id}/disconnect`, `volumes/create`.

## What is denied

Everything else — for example `swarm`, `plugins`, `secrets`, `nodes`,
`system/prune`, `images/prune`, image/volume/container `DELETE`, and
`/containers/{id}/archive` (file copy in/out of a container — the main
file-theft primitive; noted as denied until proven needed).

On `POST /containers/create` the proxy also inspects the HostConfig
and denies:

- `Privileged: true`
- binds/mounts whose host source is `/`, `/var/run/docker.sock`,
  `/run/docker.sock`, `/etc`, `/root`, `/proc`, `/sys`,
  `/var/lib/docker` (whole or in part), and `/dev` except the GPU
  device allowlist
- `PidMode` / `IpcMode` / `UTSMode` / `UsernsMode` / `CgroupnsMode`
  set to `host`
- `NetworkMode: host` (always denied; the `AllowHostNetwork` policy field is not wired to any setting).
- `CapAdd` entries outside the allowlist (default:
  `NET_BIND_SERVICE`, `SYS_NICE`, `IPC_LOCK`; the list is compiled into the
  worker and has no operator setting)
- `SecurityOpt` containing `unconfined`
- a `Runtime` other than `nvidia`, and non-nvidia GPU
  `DeviceRequests`

`POST /build` is allowed (build tasks need it) but denied when the
`networkmode=host` query param is set. Exec creates with
`Privileged: true` are denied.

## GPU passthrough

GPU work is deliberately preserved:

- `DeviceRequests` with driver `nvidia` (or the `["gpu"]` capability
  list) pass through.
- `Runtime: "nvidia"` is allowed.
- Host devices `/dev/dri*`, `/dev/kfd`, `/dev/dxg`, `/dev/nvidia*`
  may be passed into a container.
- Read-only binds of `/usr/lib/wsl` (WSL's GPU libraries) are
  allowed, as with any other non-reserved host path.

## Logging

Every denied (enforce) or would-be-denied (report) request logs one
structured line with `method`, `path`, `mode`, and `reason`. Request
bodies are never logged.
