# CORE/DockerContainers/encrypted-store/valkey-enc/Dockerfile - ENC-IMAGES
# (owner 11-10: "redesign it").
#
# Valkey that encrypts ITSELF at rest: gocryptfs is mounted INSIDE this container
# over /data, so the compose needs NO `:rshared`/`:rslave` propagation and NO
# separate sidecar service. Cross-container mount propagation does not exist on
# Docker Desktop / WSL2 (results-1110/SIDECAR-WSL2-PROBE.md: "path ... is not a
# shared mount"); a FUSE mount inside the container does (same probe: it works).
#
# SUPPLY-CHAIN PIN (same discipline as gocryptfs-sidecar/J1): the store base is
# pinned by digest (the SAME digest the CE compose already uses), the packages
# added on top are pinned by exact NEVRA and each .apk sha256 is verified BEFORE
# install (`apk add --force <local path>` - the CDN is never consulted twice).
# Checksums computed on the Dell (rootless Docker) 2026-11-10 against
# https://dl-cdn.alpinelinux.org/alpine/v3.24/{main,community}/x86_64/ - i.e. the
# release the PINNED valkey base actually is (`/etc/alpine-release` -> 3.24.2),
# NOT the 3.19 release the sidecar image uses. Any mismatch FAILS THE BUILD.
# See README.md for the pin table.
#
# WHY THE PACKAGE RELEASES DIFFER FROM versitygw-enc: the valkey base is Alpine
# 3.24.2, and installing a 3.19 `fuse3` .apk on it fails ("unable to select
# packages: so:libfuse3.so.3 (no such package)" - the base already carries a
# newer fuse3 soname). So each context pins the packages of ITS OWN base release:
# gocryptfs here is v2.6.1 (the only gocryptfs in v3.24), versitygw-enc stays on
# the 3.19 release with gocryptfs 2.4.0 exactly as J1 pinned. Both are recorded
# in README.md; the entrypoint uses only options both accept.
#
# `setpriv` (the capability drop) is already a REAL util-linux 2.42.3 binary at
# /bin/setpriv in this base (verified on the Dell: 34864 bytes, `setpriv from util-linux
# 2.42.3`, and there is NO /usr/bin/setpriv here) - unlike the 3.19 base, where /bin/setpriv
# is a busybox symlink that rejects --reuid/--bounding-set. util-linux is still installed
# here on purpose: it is one of the three pinned packages of THIS base release (gocryptfs,
# fuse3, util-linux), not a 4th package added for setpriv.
#
# NO SECRETS: the passphrase is a host-owned 0600 file bind-mounted read-only at
# /run/secrets/gocryptfs-pass at RUN time only - never a build ARG, never ENV,
# never a layer. The store runs as valkey's own uid 999 / gid 1000 (verified on the
# Dell: `id valkey` -> uid=999 gid=1000) with CAP_SYS_ADMIN removed from its
# bounding set; only the root gocryptfs daemon holds mount privileges.

# ---- pinned store base (the digest the CE compose already uses) ---------------
ARG VALKEY_BASE=valkey/valkey:8-alpine@sha256:081c2f5cb575efc901aa80ff9cdbd1ec6a301682fd35e1ebb4b0990a4a4a8507
FROM ${VALKEY_BASE}

# Global ARGs are not in scope inside a stage unless re-declared WITHOUT a value;
# re-declaring inherits the pinned default, so the pins above stay the source of
# truth.
# The PACKAGE pins are stage-scoped on purpose: only this stage installs them.
# (The BASE pin above is a global ARG, declared before the FROM.)
ARG GOCRYPTFS_NVR=gocryptfs-2.6.1-r7
ARG FUSE3_NVR=fuse3-3.18.3-r0
ARG UTIL_LINUX_NVR=util-linux-2.42.3-r1
ARG GOCRYPTFS_SHA256=43a943b1f7f66654152d27f924c30c03cf303378038fd004d974d1a834e85402
ARG FUSE3_SHA256=75c11b645c916959c38d87e8418ef20dd1d8f49468fbdf78a3fc0f14b72f4d15
ARG UTIL_LINUX_SHA256=eb9246e5216823d5a506e22aaa03f9aead08415f9bdd12b080209e9aba4d8908

# Fetch each pinned .apk once, verify its sha256, install ONLY from the verified
# local files. The base is a different Alpine release; `apk add --force <path>`
# installs exactly the verified bytes, so the release skew cannot change what
# lands (and the guard below fails the build if a tool is missing).
RUN set -eu; \
    wget -q -O /tmp/g.apk "https://dl-cdn.alpinelinux.org/alpine/v3.24/community/x86_64/${GOCRYPTFS_NVR}.apk"; \
    wget -q -O /tmp/f.apk "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/${FUSE3_NVR}.apk"; \
    wget -q -O /tmp/u.apk "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/${UTIL_LINUX_NVR}.apk"; \
    echo "${GOCRYPTFS_SHA256}  /tmp/g.apk" | sha256sum -c -; \
    echo "${FUSE3_SHA256}  /tmp/f.apk" | sha256sum -c -; \
    echo "${UTIL_LINUX_SHA256}  /tmp/u.apk" | sha256sum -c -; \
    apk add --force /tmp/g.apk /tmp/f.apk /tmp/u.apk; \
    rm -f /tmp/g.apk /tmp/f.apk /tmp/u.apk; \
    [ -x /usr/bin/gocryptfs ]; [ -x /usr/bin/fusermount3 ]; \
    [ -x /bin/mountpoint ]; [ -x /bin/setpriv ]

# Fixed in-container paths: /ciphertext-raw = host cipher bind; /data = the gocryptfs
# view (Valkey's dir) created and mounted INSIDE this container. The base already
# ships /usr/local/bin/valkey-server (static, uid-999-owned dirs) - nothing else is
# reused from it.
RUN mkdir -p /ciphertext-raw /data

# The shared entrypoint lives at the CONTEXT ROOT (this Dockerfile is built with
# -f valkey-enc/Dockerfile from the encrypted-store/ context).
COPY --chmod=755 entrypoint-enc.sh /usr/local/sbin/encstore-entrypoint.sh

# The entrypoint needs mount privileges (SYS_ADMIN + /dev/fuse + apparmor:unconfined
# come from the compose); the store process below drops them.
ENV MCW_ENCSTORE=valkey

# verify with: docker run --rm --entrypoint /usr/local/bin/valkey-server <image> --version
# (the default entrypoint needs the passfile and refuses to start without it); the
# compose's own `command:` args (`valkey-server --requirepass ...`) are passed through
# to the store.
ENTRYPOINT ["/usr/local/sbin/encstore-entrypoint.sh"]
