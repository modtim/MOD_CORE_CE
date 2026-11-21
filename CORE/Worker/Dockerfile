# Multi-stage build: cross-compile the Go binary for linux/amd64,
# linux/arm64, windows/amd64, darwin/amd64 and darwin/arm64, then copy
# all release artefacts into a minimal runtime image.
#
# The final image carries ALL FIVE platform binaries under /releases/ —
# the embedded worker (which always runs on the control server and has
# admin MinIO creds) uses them to automatically publish the current
# release to the worker-releases MinIO bucket on startup. Remote
# workers then pick up the new binary via self-update (linux only —
# windows/darwin binaries are pulled once via the /workers/agent/download
# token flow, see CORE/API/routers/worker_provisioning.py).
#
# No humans run any release script. Every rebuild of this image is a
# release.

FROM golang:1.25-alpine AS builder

# ca-certificates lets module fetches work when cross-compiling with
# deps that resolve via HTTPS.
RUN apk add --no-cache ca-certificates

WORKDIR /build
# FX.147.p2b — hash-pinned Go deps: -mod=readonly refuses to mutate go.mod
# or fetch unlisted modules, so the build uses exactly what go.sum pins.
# GOFLAGS here is Dockerfile build config (not app env): it applies to every
# go invocation in this stage and the stages that FROM it.
ENV GOFLAGS=-mod=readonly
COPY go.mod go.sum ./
RUN go mod download && go mod verify

COPY . .

# Optional release public key embed. internal/selfupdate/key.go go:embeds
# internal/selfupdate/release_pub.pem (a committed placeholder comment, no
# "-----BEGIN PUBLIC KEY-----" block, so HasReleaseKey() is false and
# self-update stays fail-closed by default). Because the embed is go:embed
# on the FILE, overwriting it here before any `go build` below covers every
# cross-compiled target with one change. This is a PUBLIC key — safe as a
# plain --build-arg. When the arg is empty (the default: local/dev builds,
# CI builds that don't pass it), the committed placeholder ships unchanged —
# not a regression, an unkeyed build must never let a worker accept an
# update. We always log which path was taken (never fail the build either
# way) so a release build silently shipping the placeholder is visible in
# the build log instead of only surfacing later as a worker heartbeat error.
ARG RELEASE_PUB_KEY_PEM
RUN if [ -n "$RELEASE_PUB_KEY_PEM" ]; then \
      printf '%s\n' "$RELEASE_PUB_KEY_PEM" > internal/selfupdate/release_pub.pem; \
    fi; \
    if grep -q -- '-----BEGIN PUBLIC KEY-----' internal/selfupdate/release_pub.pem 2>/dev/null; then \
      echo "release_pub.pem: real PUBLIC KEY block present — self-update verification will be ENABLED in this build"; \
    else \
      echo "release_pub.pem: no PUBLIC KEY block (placeholder) — self-update verification stays DISABLED in this build"; \
    fi

# The version baked into the binary comes from CORE/Worker/VERSION —
# a single human-editable file containing one semver string (e.g.
# "v0.1.0"). Bump that file when you want to ship a new release; every
# rebuild of this image picks it up automatically and the embedded
# worker publishes to MinIO on startup (see
# internal/releases/publish.go).
#
# The VERSION build arg is still honoured as an override (useful for
# CI, local experiments, or pinning a specific version at deploy time
# from docker compose), but the committed VERSION file is the default
# source of truth so that ordinary rebuilds don't need any out-of-band
# input. Placeholder values ("docker", "dev", empty) are refused by the
# release publisher so nothing accidentally ships.
ARG VERSION
RUN VERSION="${VERSION:-$(cat /build/VERSION 2>/dev/null | head -n1 | tr -d '[:space:]' || echo dev)}" && \
    echo "$VERSION" > /VERSION && \
    echo "Building worker release: $VERSION"

# GY.C2b — FIPS 140-3 build switch. The DEFAULT (empty) is the stock
# build, byte-for-byte what shipped before this arg existed. A
# non-empty value — `docker build --build-arg GOFIPS140=v1.0.0` — sets
# GODEBUG=fips140=only as the build-time setting, which the go tool
# (1.23+) bakes into every binary built in this stage as the default
# runtime GODEBUG. That puts the standard crypto package on the FIPS
# 140-3 module shipped with Go 1.24+ (go.dev/doc/fips140) and "only"
# additionally refuses non-approved algorithms. The value itself is a
# release-pipeline marker (which module version to build against); the
# mode a worker actually reports on its heartbeat ("on"/"off"/"only")
# comes from the running binary, not this arg. The egress-proxy stage
# (FROM builder) inherits it, so a FIPS build is uniformly FIPS across
# every binary in the release.
ARG GOFIPS140
ENV GODEBUG=${GOFIPS140:+fips140=only}

# Build both linux binaries. The runtime image only needs one to run
# (matching its native arch) but publishes both to MinIO so remote
# workers of either arch can self-update.
RUN VERSION=$(cat /VERSION) && \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
      -ldflags="-s -w -X main.Version=${VERSION}" \
      -o /releases/mod-worker-linux-amd64 ./cmd/worker && \
    CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build \
      -ldflags="-s -w -X main.Version=${VERSION}" \
      -o /releases/mod-worker-linux-arm64 ./cmd/worker

# The host-exec helper (opsx worker-host-exec, hx.4/hx.7a) is NOT run inside
# the worker container: the installer (hx.9) extracts it with `docker create`
# + `docker cp ...:/usr/local/bin/mod-host-exec` and runs it on the HOST,
# where it listens on the bind-mounted /run/mod-host-exec/host-exec.sock.
# It ships in the image only so installers can pull it out of it. No -X
# main.Version: the helper has no Version var, so the worker's version stamp
# would be silently ignored by go.
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
      -ldflags="-s -w" \
      -o /releases/mod-host-exec-linux-amd64 ./cmd/host-exec-helper && \
    CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build \
      -ldflags="-s -w" \
      -o /releases/mod-host-exec-linux-arm64 ./cmd/host-exec-helper

# Cross-build the desktop-install targets too (windows/amd64,
# darwin/amd64, darwin/arm64). CGO stays disabled so no C toolchain is
# needed for any target. These binaries never self-update — they're
# fetched once via the /workers/agent/download token flow (GP.6/GP.7)
# — but the embedded worker still needs them on disk to publish. Names
# MUST match CORE/API/routers/worker_provisioning.py's
# _worker_object_name() exactly (windows keeps the `.exe` extension;
# linux/darwin carry none), and CORE/Worker/Makefile's matching targets.
RUN VERSION=$(cat /VERSION) && \
    CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build \
      -ldflags="-s -w -X main.Version=${VERSION}" \
      -o /releases/mod-worker-windows-amd64.exe ./cmd/worker && \
    CGO_ENABLED=0 GOOS=darwin GOARCH=amd64 go build \
      -ldflags="-s -w -X main.Version=${VERSION}" \
      -o /releases/mod-worker-darwin-amd64 ./cmd/worker && \
    CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 go build \
      -ldflags="-s -w -X main.Version=${VERSION}" \
      -o /releases/mod-worker-darwin-arm64 ./cmd/worker

# Choose the binary matching the current platform to be the primary
# /usr/local/bin/mod-worker entrypoint. Most deployments are amd64;
# arm64 override via `docker build --platform=linux/arm64`.
RUN cp /releases/mod-worker-linux-$(apk --print-arch | sed 's/x86_64/amd64/;s/aarch64/arm64/') \
       /mod-worker && \
    cp /releases/mod-host-exec-linux-$(apk --print-arch | sed 's/x86_64/amd64/;s/aarch64/arm64/') \
       /mod-host-exec

# --- egress proxy (GB.EGR.D.4) ---
# Static build into FROM scratch. No /etc dependencies, so it works as
# a sidecar in an isolated network. Runs as uid 65534; binding :53 needs
# NET_BIND_SERVICE, which the runtime grants via HostConfig.CapAdd.
FROM builder AS builder-egress
RUN CGO_ENABLED=0 GOOS=linux GOARCH=$(apk --print-arch | sed 's/x86_64/amd64/;s/aarch64/arm64/') go build \
      -ldflags="-s -w" \
      -o /mod-egress-proxy ./cmd/egress-proxy

FROM scratch AS egress-proxy
USER 65534
COPY --from=builder-egress /mod-egress-proxy /mod-egress-proxy
# --interval/--start-period/--start-interval/--timeout/--retries mirror the
# explicit container.Config.Healthcheck the executor sets on every proxy
# container it creates (internal/executor/egress_proxy.go,
# egressProxyHealthcheck) — that override is what production relies on to get
# the daemon's first probe firing in ~1s instead of Docker's 30s built-in
# default. These flags are defence in depth for the rare case something
# constructs this image's container without going through the executor.
HEALTHCHECK --interval=2s --start-period=10s --start-interval=250ms --timeout=2s --retries=3 \
  CMD ["/mod-egress-proxy","-healthcheck"]
ENTRYPOINT ["/mod-egress-proxy"]

# docker CLI + compose plugin (add-persistent-stacks). The compose-runner
# (internal/executor/stackrunner.go) shells out to `docker compose` against the
# mounted host socket to deploy/tear down persistent stacks. Static binaries
# pulled from the official docker:cli image — no apt repo dance needed.
FROM docker:27-cli AS dockercli

# Runtime: minimal image that carries ALL FIVE platform binaries under
# /releases/ so the embedded worker can publish the full release on
# startup.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /mod-worker            /usr/local/bin/mod-worker
# The host-exec helper lives here ONLY so the installer can `docker cp` it out
# (hx.9); it is never executed inside this container. See the builder-stage
# comment above for why it is built at all.
COPY --from=builder /mod-host-exec         /usr/local/bin/mod-host-exec
COPY --from=builder /releases/             /releases/
COPY --from=builder /VERSION               /releases/VERSION

# docker CLI + compose plugin for the compose-runner.
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-compose \
                      /usr/local/libexec/docker/cli-plugins/docker-compose

# Entrypoint wrapper — makes self-update actually work for the embedded
# (in-compose) worker.
#
# Problem without this wrapper: the in-image binary at
# /usr/local/bin/mod-worker is overwritten successfully by selfUpdate()
# inside the running container, but when the container restarts Docker
# re-mounts the image layer, which ships the ORIGINAL binary. The
# worker heartbeats at the old version, the server flags
# update_requested again, the worker drains and self-updates again,
# and the cycle never completes.
#
# Fix: run the worker from a persistent-volume path instead. The
# compose file mounts a named volume at /var/lib/mod-worker/bin so
# whatever selfUpdate() writes there survives container restarts.
# At boot we compare the image binary (operator intent: they rebuilt
# the image) with the persistent binary (self-update intent: running
# workers updated themselves) and run whichever is newer. Image wins
# on rebuild, persistent wins on self-update — both paths are
# idempotent.
COPY scripts/entrypoint.sh /usr/local/bin/mod-worker-entrypoint
RUN chmod +x /usr/local/bin/mod-worker-entrypoint

# C9S3-FIX3A: create a named group+user for the container; chown worker spool
# dirs at build time so the entrypoint's `mkdir -p` at :33 does not fail with
# Permission denied on a fresh root:root named volume.
RUN groupadd -g 10001 app && \
    useradd -u 10001 -g app -s /bin/sh -d /var/lib/mod-worker app && \
    mkdir -p /var/lib/mod-worker/bin /var/lib/mod-worker/outbox /var/lib/mod-worker/host-exec /var/lib/mod-worker/stacks && \
    chown 10001:10001 /var/lib/mod-worker/bin /var/lib/mod-worker/outbox /var/lib/mod-worker/host-exec /var/lib/mod-worker/stacks

USER 10001:10001

ENTRYPOINT ["/usr/local/bin/mod-worker-entrypoint"]
