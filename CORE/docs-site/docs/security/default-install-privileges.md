---
sidebar_position: 45
slug: /security/default-install-privileges
title: What the Default Install Holds
description: "The privileges the default MOD Community Edition install gives its containers: the Docker socket held by the embedded worker, the two encrypted FUSE stores, and the services that run as root."
---

# What the Default Install Holds

This page lists, in one place, the elevated access that a default install
(the installer turns on the `builder` profile) gives to its containers. It
describes what the shipped `docker-compose.yml` does today. Read it before
you place MOD on a host that also runs other workloads.

## The Docker socket

The embedded worker (`mod-worker`) bind-mounts `/var/run/docker.sock`
read-write and runs with user id 0 and the host's Docker group id
(`DOCKER_GID`). It needs the socket to run your nodes and to manage stack
services on the machine.
The node builder is on by default: when no `COMPOSE_PROFILES` is set, the installer
writes `COMPOSE_PROFILES=builder` into `.env`, so the BuildKit builder sidecars run on a default install. Remove
`builder` from `COMPOSE_PROFILES` in `.env` to turn them off. The builder does not
mount the host Docker socket. It runs BuildKit as a non-root user (uid 1000,
rootless) and is not privileged. Its firewall sidecar `buildkitd-net` is started
with the `NET_ADMIN` capability (all other capabilities dropped, the filesystem
mounted read-only), and `buildkitd` adds `SETUID` and `SETGID` (all other
capabilities dropped) and runs under a custom seccomp policy plus an AppArmor
profile installed on the host.

If you want the daemon calls from tasks and stack services checked against an
allowlist, see [Docker Socket Proxy](/security/docker-socket-proxy). The proxy
is off by default.

## The two encrypted FUSE stores

Two default services keep their data encrypted at rest with a gocryptfs
filesystem that they mount inside their own container:

- `redis` (the Valkey cache, image `valkey-enc`)
- `versitygw` (the object store, image `versitygw-enc`)

To mount a FUSE filesystem, each of these services is started with the
`SYS_ADMIN` capability, the `/dev/fuse` device and the `apparmor:unconfined`
security option. Each one also bind-mounts its ciphertext directory from the
host, read-write, under `/var/lib/mod/` (by default
`/var/lib/mod/valkey-ciphertext` and `/var/lib/mod/versitygw-ciphertext`), and
reads the store passphrase from a file under `/var/lib/mod/`. The compose file
notes that each entrypoint drops `SYS_ADMIN` before the store process starts.
The container itself is still created with the capability and the unconfined
profile.

## Images that run as root

Some service containers run as root inside their containers. The compose file
sets user 0 for exactly five services:

- `mod-worker` (user 0 with the Docker group id, see above)
- `openbao-init`
- `openbao-unseal` (unseals the secrets vault at startup)
- `openbao-data-perms` (fixes volume permissions for the vault)
- `registry-init`

The MOD-built dashboard, docs-site, buildkit-fw, valkey-enc, versitygw-enc,
video-converter-h264 and ffmpeg_cpu-h264 images set no `USER` in their
Dockerfiles. The dashboard and docs-site images build on nginx, buildkit-fw
and versitygw-enc on alpine, and the two h264 images on debian. Those bases
default to root, so these images run as root. The two h264 images exist only
when the `h264` profile is on. The valkey-enc image builds on the upstream
valkey image, and this page does not check that image for a `USER` line.

If your host policy does not allow a Docker socket, FUSE with `SYS_ADMIN`, or
root in containers, check that policy against this list before you install.
