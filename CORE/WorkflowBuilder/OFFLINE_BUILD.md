# WorkflowBuilder — offline (airgap) Docker build

The target box has **no registry egress** from the Docker/WSL layer: `npm`,
`pnpm install`, and `corepack prepare` all fail (`EAI_AGAIN` / timeout) inside
build containers, even with `--network=host`. This note explains how to build
the WorkflowBuilder image with **zero network access**.

## TL;DR

```bash
# Build offline (no registry access at all):
docker build --network=none \
  --build-arg DEPS_IMAGE=mod-workflow-builder-deps:offline \
  --build-arg NEXT_PUBLIC_API_URL="https://example.com" \
  --build-arg NEXT_PUBLIC_DASHBOARD_URL="https://example.com" \
  -t mod-workflow-builder:latest CORE/WorkflowBuilder

# Redeploy (recreates the workflow_builder container from the built image):
CORE/startup/redeploy-frontend.sh wb
```

## How it works

The `Dockerfile` takes a `DEPS_IMAGE` build arg (default `node:20-alpine`):

- **Online path (default):** `DEPS_IMAGE=node:20-alpine`. The `deps` stage runs
  `npm install -g pnpm@9 && pnpm install` from the registry, and the `builder`
  stage installs pnpm the same way. Identical to the historical build.
- **Offline path:** `DEPS_IMAGE=mod-workflow-builder-deps:offline`. That base
  image already contains `/app/node_modules` (full dev toolchain, alpine/musl)
  and a global `pnpm@9`. The `deps` stage's install is guarded by
  `if [ ! -d node_modules ]` (no-op), and the `builder` stage's pnpm install is
  guarded by `if ! command -v pnpm` (no-op). Nothing touches the network, so it
  builds under `--network=none`.

`next.config.mjs` uses `output: 'standalone'` with `eslint`/`typescript`
`ignoreDuringBuilds`, and the app source imports no `next/font/google` — so
`next build` itself makes no network calls.

## The offline deps base image (`mod-workflow-builder-deps:offline`)

This is the one artifact the offline build depends on. It is a Docker **image on
the box** (not committed to git — `node_modules` is ~870 MB). It was produced
from the existing BuildKit layer cache with **no downloads**:

```bash
# Reproduces the deps layer purely from cache (default network, but zero
# registry traffic because the RUN layer is CACHED), then tag it:
docker build --target deps -t mod-workflow-builder-deps:offline CORE/WorkflowBuilder
```

> Note: `--network=none` changes BuildKit's RUN cache key, so the cache only
> re-hits under the default network mode. No packages are fetched — the deps
> `RUN` shows `CACHED`. If you ever see it actually running `npm install`, the
> cache was evicted and you need connectivity (or a restored base image, below)
> to regenerate it.

### Keep it alive in the field

Docker's image store persists the base across reboots, but a `docker system
prune -a` (or a fresh box) would remove it. To make the airgap build survive
that, save the base image to a tarball kept on the box:

```bash
docker save mod-workflow-builder-deps:offline -o /path/on/box/wb-deps-offline.tar   # ~870 MB
# restore later:
docker load -i /path/on/box/wb-deps-offline.tar
```

As long as `mod-workflow-builder-deps:offline` is loadable, the offline build
above works with no internet, forever.
