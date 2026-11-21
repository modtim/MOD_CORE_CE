#!/bin/sh
# mod-worker entrypoint — picks the right binary at container start so
# selfUpdate() survives restarts.
#
# Why this exists: the binary baked into the docker image lives on a
# read-only layer that gets re-mounted every time the container
# restarts. Self-update's os.Rename() modifies the container's writable
# layer, but that layer is thrown away at restart — so without a
# persistent volume the updated binary never takes effect and the
# worker loops forever (drain → self-update → restart → drain → ...).
#
# Resolution: the compose file mounts a named volume at
# /var/lib/mod-worker/bin. This script:
#   1. Bootstraps that volume from the image on first boot.
#   2. On every boot, compares image version with persistent version
#      and runs whichever is newer:
#        - image  newer  →  operator rebuilt compose (authoritative)
#        - persist newer →  self-update wrote a newer binary
#        - same version, different bytes → image rebuilt without a VERSION
#          bump: the image wins (a self-update never writes the same version)
#      Both cases resolve deterministically.
#   3. exec's the chosen binary so the Go worker sees os.Executable()
#      as the persistent-volume path (so its own self-update writes
#      there, preserving the cycle next time).

set -eu

# Overridable only so scripts/tests/entrypoint_test.sh can drive the decision logic;
# the defaults are the real image/volume paths.
IMAGE_BIN="${MOD_WORKER_IMAGE_BIN:-/usr/local/bin/mod-worker}"
PERSIST_DIR="${MOD_WORKER_PERSIST_DIR:-/var/lib/mod-worker/bin}"
PERSIST_BIN="${PERSIST_DIR}/mod-worker"

# Install the image binary at the persistent path atomically: write a temp file
# in the same directory, then rename over the target. A rename over a file
# another process is still executing is allowed on Linux (the old inode lives on
# until it is released); opening that file for writing would fail with ETXTBSY
# ("Text file busy") and, under set -e, crash-loop the container.
install_image_bin() {
    tmp="$(mktemp "${PERSIST_DIR}/.mod-worker.XXXXXX")" || {
        echo "entrypoint: FATAL: cannot create a temp file in ${PERSIST_DIR} (volume full or read-only?)" >&2
        return 1
    }
    if cp "${IMAGE_BIN}" "$tmp" &&
       chmod 0755 "$tmp" &&
       mv -f "$tmp" "${PERSIST_BIN}"; then
        return 0
    fi
    rm -f "$tmp"
    echo "entrypoint: FATAL: could not install ${IMAGE_BIN} at ${PERSIST_BIN}" >&2
    return 1
}

# Nothing at the persistent path yet — first boot (or fresh volume).
# Copy the image binary out so self-update has something to rename over
# later without cross-filesystem restrictions.
mkdir -p "${PERSIST_DIR}"
if [ ! -x "${PERSIST_BIN}" ]; then
    install_image_bin
fi

# Decide which binary to run. `--version` is a cheap no-side-effect
# probe (exits 0 with the version string on stdout for both variants).
# sort -V does semver-aware ordering; tail -1 yields the newer value.
image_ver="$(${IMAGE_BIN} --version 2>/dev/null | awk '{print $NF}' | head -1 || echo '')"
persist_ver="$(${PERSIST_BIN} --version 2>/dev/null | awk '{print $NF}' | head -1 || echo '')"

if [ -z "${persist_ver}" ] || [ "${persist_ver}" = "dev" ]; then
    # Persistent binary is missing --version or marked dev (unusable).
    # Prefer the image copy in that case.
    install_image_bin
elif [ -n "${image_ver}" ] && [ "${image_ver}" != "${persist_ver}" ]; then
    newer="$(printf '%s\n%s\n' "${image_ver}" "${persist_ver}" | sort -V | tail -1)"
    if [ "${newer}" = "${image_ver}" ]; then
        # Image is newer — operator rebuilt the compose image with a
        # bumped VERSION. Overwrite the persistent copy so future
        # self-updates compare against the new baseline.
        install_image_bin
    fi
    # else: persistent is newer — self-update already wrote it, keep it.
elif [ -n "${image_ver}" ] && ! cmp -s "${IMAGE_BIN}" "${PERSIST_BIN}"; then
    # SAME version string but DIFFERENT bytes: the operator rebuilt the image
    # without bumping CORE/Worker/VERSION (BUG.WORKER-STALE-BINARY: the Dell ran
    # a 5-day-old cached binary under every rebuilt image). A self-update only
    # ever installs a HIGHER release version, so it can never produce this state:
    # the rebuilt image wins. A genuine self-update keeps winning through the
    # version comparison above.
    install_image_bin
fi

exec "${PERSIST_BIN}" "$@"
