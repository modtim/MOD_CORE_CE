# CORE/DockerContainers/encrypted-store/versitygw-enc/Dockerfile - ENC-IMAGES
# (owner 11-10: "redesign it").
#
# versitygw S3 store that encrypts ITSELF at rest: gocryptfs is mounted INSIDE this
# container over /data, so the compose needs NO `:rshared`/`:rslave` propagation and
# NO separate sidecar service. That is the whole point: cross-container mount
# propagation does not exist on Docker Desktop / WSL2 (results-1110/SIDECAR-WSL2-PROBE.md),
# while a FUSE mount inside the container does (same probe: it works).
#
# SUPPLY-CHAIN PIN (same discipline as gocryptfs-sidecar/J1, but for TWO bases):
#  * the alpine+gocryptfs base is pinned by digest;
#  * the versitygw BINARY is copied out of the upstream digest-pinned image
#    (`COPY --from=`), so the binary is upstream v1.7.0 exactly, not a rebuild;
#  * every package is pinned by exact NEVRA and its .apk sha256 is verified BEFORE
#    install (`apk add --force <local path>` - the CDN is never consulted twice).
# Checksums were computed on the Dell (rootless Docker) on 2026-11-10 against
# https://dl-cdn.alpinelinux.org/alpine/v3.19/{main,community}/x86_64/. Any mismatch
# FAILS THE BUILD. See README.md for the pin table.
#
# versitygw has NO built-in encryption (owner-confirmed; the store is a plain POSIX
# backend over /data) - the encryption here is gocryptfs, nothing else.
#
# NO SECRETS: the passphrase is a host-owned 0600 file bind-mounted read-only at
# /run/secrets/gocryptfs-pass at RUN time only - never a build ARG, never ENV, never
# a layer. The store runs as uid 1000 with CAP_SYS_ADMIN removed from its bounding
# set; only the gocryptfs daemon (root) holds mount privileges.

# BOTH bases are declared BEFORE the first FROM, i.e. they are GLOBAL ARGs: a later
# stage's `FROM ${...}` sees them (an ARG declared after the first FROM is stage-
# scoped and would leave the second FROM blank - the build would fail).
ARG VGW_BASE=ghcr.io/versity/versitygw:v1.7.0@sha256:c4cbd9d9cb8dedbb055ac788dbd02635651b9b1cebac95b095b3217231aa87ad
ARG ALPINE_BASE=alpine:3.19@sha256:6baf43584bcb78f2e5847d1de515f23499913ac9f12bdf834811a3145eb11ca1

# ---- stage 1: the upstream store binary, pinned by digest ---------------------
FROM ${VGW_BASE} AS storebin
# Inspected on the Dell 2026-11-10: the image is a static Go binary
# (`file` -> "Not a valid dynamic program", i.e. no libc/libfuse dependency to
# carry), binary at /usr/local/bin/versitygw, `versitygw --version` -> v1.7.0,
# image USER is root (uid 0). Nothing else from that image is reused.
RUN echo "stage guard: the pinned image must carry the binary" && \
    [ -x /usr/local/bin/versitygw ]

# ---- stage 2: pinned alpine + gocryptfs base, then the binary on top ---------
# The BASE ARGs are GLOBAL (declared above the first FROM) and stay visible to
# every stage's FROM line - re-declaring them here would shadow the pin with a
# stage-scoped copy, so the pin lives exactly once.
FROM ${ALPINE_BASE}

# The PACKAGE pins are stage-scoped on purpose: only this stage installs them.
# (The two BASE pins are global ARGs above the first FROM - a stage-scoped ARG
# would leave the second FROM blank.)
ARG GOCRYPTFS_NVR=gocryptfs-2.4.0-r7
ARG FUSE3_NVR=fuse3-3.16.2-r0
ARG UTIL_LINUX_NVR=util-linux-2.39.3-r0
ARG GOCRYPTFS_SHA256=d914e4952fa60bf76a6781b591eb87eeac6af89ae94a668ac79b648b3f310a5c
ARG FUSE3_SHA256=72009a8b616e8ae598658e947ca4321f9662c61be08833fce333dde77ea0f718
ARG UTIL_LINUX_SHA256=3b4981db887692469b28eaf89a0409665404b38f9bde53b6de5ef3ecff9dd28e

# Fetch each pinned .apk once, verify its sha256, install ONLY from the verified
# local files. `setpriv` (the capability drop) is NOT usable from the base here:
# /bin/setpriv in this 3.19 base is a busybox symlink that rejects --reuid and
# --bounding-set outright. The pinned util-linux apk (the 3rd package, not a 4th)
# installs the real util-linux setpriv at /usr/bin/setpriv (2.39.3, first on PATH);
# proven on the Dell: `setpriv --bounding-set=-sys_admin` clears CapBnd bit 21.
RUN set -eu; \
    wget -q -O /tmp/g.apk "https://dl-cdn.alpinelinux.org/alpine/v3.19/community/x86_64/${GOCRYPTFS_NVR}.apk"; \
    wget -q -O /tmp/f.apk "https://dl-cdn.alpinelinux.org/alpine/v3.19/main/x86_64/${FUSE3_NVR}.apk"; \
    wget -q -O /tmp/u.apk "https://dl-cdn.alpinelinux.org/alpine/v3.19/main/x86_64/${UTIL_LINUX_NVR}.apk"; \
    echo "${GOCRYPTFS_SHA256}  /tmp/g.apk" | sha256sum -c -; \
    echo "${FUSE3_SHA256}  /tmp/f.apk" | sha256sum -c -; \
    echo "${UTIL_LINUX_SHA256}  /tmp/u.apk" | sha256sum -c -; \
    apk add --force /tmp/g.apk /tmp/f.apk /tmp/u.apk; \
    rm -f /tmp/g.apk /tmp/f.apk /tmp/u.apk; \
    [ -x /usr/bin/gocryptfs ]; [ -x /usr/bin/fusermount3 ]; \
    [ -x /bin/mountpoint ]; [ -x /bin/setpriv ]

# The pinned upstream binary, copied out of the digest-pinned image.
COPY --from=storebin /usr/local/bin/versitygw /usr/local/bin/versitygw

# Fixed in-container paths: /ciphertext-raw = host cipher bind; /data = the gocryptfs
# view (the store's dir) created and mounted INSIDE this container; /iam = the CE
# IAM named volume (tenant ACLs), no propagation flag anywhere.
RUN mkdir -p /ciphertext-raw /data /iam

# The store user is NUMERIC uid/gid 1000 (versitygw's upstream image runs as root,
# so CE fixes the uid here): `setpriv --reuid=1000 --regid=1000` needs no /etc/passwd
# entry, so the image carries no extra user - fewer moving parts, and the cipher dir
# must be 0700 owned by the store uid (the CE default 0700 root-owned is fixed by the
# entrypoint's ownership prep before the mount; a 0777 host dir also works but is not
# CE-exact - see README).

# The shared entrypoint lives at the CONTEXT ROOT (this Dockerfile is built with
# -f versitygw-enc/Dockerfile from the encrypted-store/ context).
COPY --chmod=755 entrypoint-enc.sh /usr/local/sbin/encstore-entrypoint.sh

# The entrypoint needs mount privileges (SYS_ADMIN + /dev/fuse + apparmor:unconfined
# come from the compose); the store process below drops them.
ENV MCW_ENCSTORE=versitygw

# verify with: docker run --rm --entrypoint /usr/local/bin/versitygw <image> --version
# (the default entrypoint needs the passfile and refuses to start without it); the
# compose's own `command:` args (port/admin-port/iam-dir/region/posix /data) are passed
# through to the store by the entrypoint.
ENTRYPOINT ["/usr/local/sbin/encstore-entrypoint.sh"]
