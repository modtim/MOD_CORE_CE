# CORE/DockerContainers/postgres-app/Dockerfile - GY.C6.s1 (owner 11-08: "pg_tle must
# be REAL in CE, via a CUSTOM postgres image that ships pg_tle (not pgcrypto-only)").
#
# POSTGRES 17 (owner 11-09): the CE database is PostgreSQL 17 (PG13 is end-of-life; the live
# CE data dir, keycloak-db and db-owner-repair are already on the pinned postgres:17.11
# below). PG_MAJOR selects the flavour; 17 is the default and what CE ships. A 13 flavour
# still builds for anything that needs it:
#   docker build --build-arg PG_MAJOR=13 --build-arg PG_BASE=postgres:13 ...
#
# MULTI-STAGE: a builder stage COMPILES pg_tle from a pinned, checksum-verified source
# tarball; the final stage is the same stock postgres base, with only pg_tle's .so /
# .control / .sql copied in. Compilers, -dev packages and the source tree never reach the
# shipped image, and the stock docker-entrypoint.sh, its boot ordering, the stock
# `postgres` user and the /docker-entrypoint-initdb.d drop-in dir are unchanged.
#
# Why compile: there is NO pg_tle apt package (not in Debian, not in PGDG), and
# `postgresql13-contrib` is a yum name. pgcrypto already ships in the stock postgresql-NN
# package, so only pg_tle needs adding.
#
# pg_tle can only be loaded through shared_preload_libraries, so every compose that runs
# this image with pg-encryption-init.sh must preload it (CE overlay compose:
# shared_preload_libraries=pg_stat_statements,pg_tle). Without the preload
# `CREATE EXTENSION pg_tle` fails ("must be loaded via shared_preload_libraries").
#
# Supply-chain pin: aws/pg_tle v1.5.3 (supports PG 12-19; carries the RELRO and
# identifier-validation hardening v1.4.0 lacks). Pinned THREE ways, all enforced at build
# time: the release tag, the commit that tag must resolve to (read from the tarball's own
# git header), and the sha256 of the GitHub tag tarball (computed once, checked on two
# hosts). Any mismatch FAILS THE BUILD. To bump: change all three ARGs together, after
# reviewing the release.
#
# PG-MINOR PIN (FIX-POSTGRES-APP-PG-TLE, 11-15): the builder must compile pg_tle against
# the server headers of the SAME PGDG minor the base image's server package carries.
# `apt-get install postgresql-server-dev-17` UNPINNED silently drifts: PGDG moved from
# 17.11-1.pgdg13+2 to 17.12-1.pgdg13+1 between the 11-10 and 11-15 builds, so the 11-15
# image compiled pg_tle against 17.12 headers while the pinned base stayed postgres:17.11
# - pg_tle 1.5.3 then references `heap_datum_check`, which 17.11 does not export, and the
# image dies at boot: FATAL could not load library .../pg_tle.so: undefined symbol:
# heap_datum_check (proven: head-39ba2b04000a ldd -r count 1 vs head-494564420144 count
# 0). The dev package is therefore pinned to the base image's own server version, and a
# build-time guard LOADS pg_tle the way shared_preload_libraries does, so a minor
# mismatch can never ship again.
ARG PG_MAJOR=17
ARG PG_BASE=postgres:17.11@sha256:b8faeff7712663c2773a190181e1246e37e364b887611c04658c28b44e2408fe
ARG PG_TLE_VERSION=v1.5.3
ARG PG_TLE_COMMIT=6340fd2ef19a806dc0f2588d6aec10a0a12f7e8b
ARG PG_TLE_SHA256=90cd505bd2b427911f16c738bafeb3b8029d7b30194df9518270d94e06fc791e

# ---------------------------------------------------------------------------
# Stage 1: build pg_tle against the server headers of the SAME base image.
# ---------------------------------------------------------------------------
FROM ${PG_BASE} AS builder
ARG PG_MAJOR
ARG PG_TLE_VERSION
ARG PG_TLE_COMMIT
ARG PG_TLE_SHA256
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl git flex bison libkrb5-dev; \
    PG_SERVER_VER="$(dpkg-query -W "postgresql-${PG_MAJOR}" | cut -f2)"; \
    echo "base server package version: ${PG_SERVER_VER}"; \
    apt-get install -y --no-install-recommends \
        "postgresql-server-dev-${PG_MAJOR}=${PG_SERVER_VER}"; \
    [ "$(dpkg-query -W "postgresql-server-dev-${PG_MAJOR}" | cut -f2)" = "${PG_SERVER_VER}" ] \
        || { echo "postgresql-server-dev-${PG_MAJOR} is not the base server version ${PG_SERVER_VER}: $(dpkg-query -W "postgresql-server-dev-${PG_MAJOR}" | cut -f2)" >&2; exit 1; }; \
    postgres --version | grep -qE " ${PG_MAJOR}(\.| )" \
        || { echo "PG_BASE is not PostgreSQL ${PG_MAJOR}: $(postgres --version)" >&2; exit 1; }; \
    mkdir -p /src/pg_tle; \
    curl -fsSL --retry 3 -o /src/pg_tle.tar.gz \
        "https://github.com/aws/pg_tle/archive/refs/tags/${PG_TLE_VERSION}.tar.gz"; \
    echo "${PG_TLE_SHA256}  /src/pg_tle.tar.gz" | sha256sum -c -; \
    [ "$(gzip -cd /src/pg_tle.tar.gz | git get-tar-commit-id)" = "${PG_TLE_COMMIT}" ] \
        || { echo "pg_tle ${PG_TLE_VERSION} tarball is not commit ${PG_TLE_COMMIT}" >&2; exit 1; }; \
    tar -xzf /src/pg_tle.tar.gz -C /src/pg_tle --strip-components=1; \
    cd /src/pg_tle; \
    make; \
    make install; \
    test -f "/usr/share/postgresql/${PG_MAJOR}/extension/pg_tle.control"; \
    test -f "/usr/lib/postgresql/${PG_MAJOR}/lib/pg_tle.so"

# ---------------------------------------------------------------------------
# Stage 2: the shipped image - stock postgres + pg_tle files only.
# No USER change: it keeps running as the stock `postgres` user (uid 999).
# ---------------------------------------------------------------------------
FROM ${PG_BASE}
ARG PG_MAJOR

COPY --from=builder /usr/share/postgresql/${PG_MAJOR}/extension/pg_tle* /usr/share/postgresql/${PG_MAJOR}/extension/
COPY --from=builder /usr/lib/postgresql/${PG_MAJOR}/lib/pg_tle.so /usr/lib/postgresql/${PG_MAJOR}/lib/

# Fail-closed at BUILD time: pg_tle (copied) and pgcrypto (stock) must both be
# present, and pg_tle.so must have every shared-library dependency resolvable in this
# final image, so a build that lacks either can never be published as a CE image.
# No secrets in any RUN layer: no vault token, no pg_tle key material, no license key
# is ever downloaded, written or echoed here; key material is a runtime Vault mount.
RUN set -eu; \
    test -f "/usr/share/postgresql/${PG_MAJOR}/extension/pg_tle.control"; \
    test -f "/usr/lib/postgresql/${PG_MAJOR}/lib/pg_tle.so"; \
    test -f "/usr/share/postgresql/${PG_MAJOR}/extension/pgcrypto.control"; \
    test -f "/usr/lib/postgresql/${PG_MAJOR}/lib/pgcrypto.so"; \
    if ldd "/usr/lib/postgresql/${PG_MAJOR}/lib/pg_tle.so" | grep -q 'not found'; then \
        ldd "/usr/lib/postgresql/${PG_MAJOR}/lib/pg_tle.so" >&2; exit 1; \
    fi; \
    LD_PRELOAD="/usr/lib/postgresql/${PG_MAJOR}/lib/pg_tle.so" \
        /usr/lib/postgresql/${PG_MAJOR}/bin/postgres --version >/dev/null \
        || { echo "pg_tle.so cannot be loaded by the postgres executable (PGDG minor mismatch)" >&2; exit 1; }

# Tenant-scoped pg_tle key config (security must): one file per tenant, mode 0400
# inside a mode-0700 dir, mounted read-only at runtime from Vault. Tenant B cannot
# read tenant A's file. The dir is created EMPTY here - nothing is COPYed or baked -
# and no new environment variable is introduced (the platform-settings registry stays
# unchanged); the boot script sources the per-tenant file if present.
RUN install -d -m 0700 /etc/postgresql/pg_tle.d

# The GY.C6.s1 boot script is NOT COPYed into a layer: docker-compose.yml bind-mounts
# CORE/startup/boot/pg-encryption-init.sh read-only onto
# /docker-entrypoint-initdb.d/99-gy-c6s1-pg-encryption.sh, so a fresh clone's copy of
# that script (which the in-flight FIX-GY-C6S1-PG-INIT-SOURCE-SAFE job edits) is what
# runs at boot. A COPY of it here would freeze a stale copy in the image and shadow the bind.
