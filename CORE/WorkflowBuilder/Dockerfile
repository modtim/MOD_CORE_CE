# ---------------------------------------------------------------------------
# OFFLINE-CAPABLE BUILD
#
# The Docker/WSL layer on the target box has NO registry egress (airgap). To
# build with zero network access, pass a prebuilt deps base image that already
# ships /app/node_modules and a global pnpm:
#
#   docker build --network=none \
#     --build-arg DEPS_IMAGE=mod-workflow-builder-deps:offline \
#     -t mod-workflow-builder:offline CORE/WorkflowBuilder
#
# The default DEPS_IMAGE (node:20-alpine) preserves the normal ONLINE build:
# it installs pnpm + dependencies from the registry exactly as before.
#
# See OFFLINE_BUILD.md next to this Dockerfile for how the offline base image
# is produced and kept working in the field.
# ---------------------------------------------------------------------------

ARG DEPS_IMAGE=node:20-alpine

# 1. Install dependencies
FROM ${DEPS_IMAGE} AS deps
WORKDIR /app
# FX.147.p2b — the hash-pinned lockfile this build actually consumes is
# package-lock.json (lockfileVersion 3, integrity hashes on every entry);
# pnpm-lock.yaml on disk is a stub (lockfileVersion 9.0 header only, no
# packages) so it pins nothing. npm ci installs exactly what package-lock.json
# pins and verifies each integrity hash; it fails loudly if the lockfile is
# missing or out of sync with package.json.
COPY package.json package-lock.json ./
# Pin pnpm to the major that produced pnpm-lock.yaml (lockfileVersion 9.0).
# `npm install -g pnpm` (unpinned) floats to the latest tag, which broke the
# build when pnpm published a version whose tarball 404s from the registry.
# When DEPS_IMAGE is the offline base it already carries node_modules + a
# global pnpm, so this whole step is a no-op and needs NO network. Online (the
# default node:20-alpine base) it installs from the registry as before.
# FX.147.p2b — was `pnpm install --no-frozen-lockfile`, which re-resolved
# every dependency from package.json at build time (unpinned: the stub
# pnpm-lock.yaml pinned nothing, so a registry version bump silently changed
# what shipped). npm ci is lockfile-exact + integrity-verified against
# package-lock.json. `npm install -g pnpm@9` stays (a pinned GLOBAL tool for
# `pnpm build` below, not the app's dependency tree).
# --legacy-peer-deps: vaul@0.9.9 declares a peer range of react ^16.8||^17||^18
# while the root project is on react 19.2.4, so plain `npm ci` fails ERESOLVE.
# The flag only skips peer-range enforcement — the lockfile stays exact and
# integrity-verified.
RUN if [ ! -d node_modules ]; then npm install -g pnpm@9 && npm ci --legacy-peer-deps; fi

# 2. Build the application
FROM ${DEPS_IMAGE} AS builder
WORKDIR /app

# GT.B1: NEXT_PUBLIC_API_URL / NEXT_PUBLIC_DASHBOARD_URL are NO LONGER baked
# at build time. The customer domain is a runtime value: docker-entrypoint.sh
# renders public/runtime-config.js from MOD_API_URL / MOD_DASHBOARD_URL at
# container start, and the app reads it via lib/runtime-config.ts. Building
# works with zero customer-specific args; changing the domain needs only a
# container recreate, never an image rebuild.
ENV NEXT_TELEMETRY_DISABLED=1

COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Ensure pnpm is available WITHOUT hitting the registry: the offline base
# already has it (command -v pnpm succeeds -> skip install); the online base
# installs it. Then `pnpm build` runs entirely from the local node_modules.
RUN if ! command -v pnpm >/dev/null 2>&1; then npm install -g pnpm@9; fi && pnpm build

# 3. Run the application
FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
# Disable Next.js telemetry
ENV NEXT_TELEMETRY_DISABLED 1

# Copy only the necessary files for a production server
COPY --from=builder --chown=node:node /app/public ./public
COPY --from=builder --chown=node:node /app/package.json ./package.json
# Standalone output mode generates this folder
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
# R36.2 — strip sharp + @img/* (sharp-libvips is LGPL-3.0-or-later) from the
# shipped image. sharp is an OPTIONAL dependency of next@15 (package-lock.json
# "node_modules/next" -> optionalDependencies "sharp": "^0.34.1"); next pulls
# it in only inside next/dist/server/image-optimizer.js `getSharp()`, a lazy
# `require('sharp')` wrapped in try/catch, and this app never reaches that path:
# next.config.mjs sets images.unoptimized (no /_image route) and no source file
# imports sharp (the only "sharp" hits in the tree are the "csharp"/"sharpen"
# words). The standalone node_modules next copies in therefore ships dead code,
# and shipping it would put an LGPL-3.0+ component in the image. Removing it
# here (runner stage, after the standalone COPY) is the smallest change that
# covers BOTH build paths — the online `npm ci` install and the offline
# DEPS_IMAGE base that already carries node_modules — without touching the
# other next optionalDeps (@next/swc-*, which the standalone server DOES need).
RUN rm -rf /app/node_modules/sharp /app/node_modules/@img
# GT.B1: entrypoint renders public/runtime-config.js from container env
COPY --chown=node:node docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x ./docker-entrypoint.sh

USER node

# The Next.js app will run on port 3000 inside the container
EXPOSE 3000
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["node", "server.js"]
