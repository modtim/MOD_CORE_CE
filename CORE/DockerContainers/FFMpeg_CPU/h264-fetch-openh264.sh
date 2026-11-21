#!/bin/sh
# h264-fetch-openh264.sh: ENTRYPOINT wrapper shared by the two opt-in H.264 add-on images
# (video-converter/Dockerfile.h264 and FFMpeg_CPU/Dockerfile.h264; the FFMpeg_CPU copy is
# byte-identical, a test asserts it).
#
# WHY: Cisco's licence covers a libopenh264 binary that the USER'S OWN machine downloads from
# Cisco's server. So NO Cisco binary is inside our image. ffmpeg is linked against the
# codec API (soname libopenh264.so.8) and finds the library at run time through
# LD_LIBRARY_PATH=/opt/openh264 (an EMPTY directory in the image).
#
# AT START, in this order, the first that works wins:
#   1. CACHE   /opt/openh264/libopenh264.so.8 exists and its sha256 matches the pin
#              (mount a NAMED VOLUME at /opt/openh264 so it survives container re-creation).
#   2. MOUNT   H264_OPENH264_FILE=<path of a file you mounted into the container>:
#              either Cisco's libopenh264-2.7.0-linux64.8.so.bz2 or the decompressed .so.
#              Use this for an offline / air-gapped host: download the file once from
#              https://ciscobinary.openh264.org/libopenh264-2.7.0-linux64.8.so.bz2
#              (the sha256 is checked, a wrong file is refused) and mount it read-only.
#              H264_OPENH264_FILE is an image-level setting of this add-on only.
#   3. DOWNLOAD from the fixed URL below, sha256 checked BEFORE the file reaches the library path.
# A sha256 mismatch REFUSES (file removed, exit 1). If nothing works the container FAILS
# with a clear message (stderr) and a contract JSON error (stdout), exit 1. There is NO
# fallback to any software or other encoder and NO setting that disables the check.
# Then: exec "$@" (the original command of the image).
#
# Example (offline):  docker run -v openh264-cache:/opt/openh264 \
#    -v /srv/libopenh264-2.7.0-linux64.8.so.bz2:/openh264/lib.bz2:ro \
#    -e H264_OPENH264_FILE=/openh264/lib.bz2 <image>
set -u

LIB_DIR=/opt/openh264
LIB_NAME=libopenh264.so.8
URL=https://ciscobinary.openh264.org/libopenh264-2.7.0-linux64.8.so.bz2
PIN_BZ2=44ad0dd2c5af2bac84d26f89f2eb4e268708b8739323d15d33837f7221ce4980
PIN_SO=1522f873a482923c5ec018a4415e1866898197528828c50f90fd300c94620df3

fail() {
    printf 'h264 add-on: %s\n' "$1" >&2
    printf 'h264 add-on: Cisco libopenh264 2.7.0 is NOT inside this image (Cisco licence). Provide it by one of:\n' >&2
    printf '  1) let the container reach %s (first start only; mount a NAMED VOLUME at %s to keep it)\n' "$URL" "$LIB_DIR" >&2
    printf '  2) offline: download that file elsewhere, mount it into the container and set H264_OPENH264_FILE=<mounted path>\n' >&2
    printf '{"error": "h264 add-on: libopenh264 unavailable (see stderr)", "code": "INTERNAL"}\n'
    exit 1
}

sha_of() { sha256sum "$1" | cut -d' ' -f1; }

[ -d "$LIB_DIR" ] || mkdir -p "$LIB_DIR" 2>/dev/null || fail "cannot create $LIB_DIR"
# FIX-H264-FETCH-HARDENING: the temp download area is a PRIVATE directory INSIDE the cache
# volume (mode 0700: umask 077 in the subshell before any file is written; mktemp -d creates
# 0700 itself). The trap removes it on EVERY exit path (EXIT/INT/TERM), so a partially
# downloaded file is never left behind; the verified file is mv'd into place (same filesystem
# => atomic rename) only after the sha check above.
WORK_DIR=$(umask 077; mktemp -d "$LIB_DIR/.fetch-XXXXXX") || fail "cannot create private temp dir in $LIB_DIR"
trap '[ -n "${WORK_DIR:-}" ] && rm -rf "$WORK_DIR"' EXIT INT TERM
TMP="$WORK_DIR/fetch"

# install_so <file>: verify the decompressed .so pin, then put it on the library path.
install_so() {
    got=$(sha_of "$1")
    if [ "$got" != "$PIN_SO" ]; then
        rm -f "$1"
        fail "REFUSED: decompressed library sha256 $got does not match the pin $PIN_SO"
    fi
    chmod 0755 "$1" && mv -f "$1" "$LIB_DIR/$LIB_NAME" || fail "cannot install into $LIB_DIR"
}

# from_bz2 <bz2 file> <delete-on-mismatch 0|1>
from_bz2() {
    got=$(sha_of "$1")
    if [ "$got" != "$PIN_BZ2" ]; then
        [ "$2" = 1 ] && rm -f "$1"
        fail "REFUSED: sha256 $got of the .so.bz2 does not match the pin $PIN_BZ2"
    fi
    bunzip2 -c "$1" > "$TMP" 2>/dev/null || { rm -f "$TMP"; fail "bunzip2 of the library failed"; }
    install_so "$TMP"
}

if [ -f "$LIB_DIR/$LIB_NAME" ] && [ "$(sha_of "$LIB_DIR/$LIB_NAME")" = "$PIN_SO" ]; then
    :   # 1. cached, verified: no network
elif [ -n "${H264_OPENH264_FILE:-}" ]; then
    rm -f "$LIB_DIR/$LIB_NAME"
    [ -f "$H264_OPENH264_FILE" ] || fail "H264_OPENH264_FILE=$H264_OPENH264_FILE does not exist"
    case "$H264_OPENH264_FILE" in
        *.bz2) from_bz2 "$H264_OPENH264_FILE" 0 ;;
        *) cp "$H264_OPENH264_FILE" "$TMP" || fail "cannot read $H264_OPENH264_FILE"; install_so "$TMP" ;;
    esac
else
    rm -f "$LIB_DIR/$LIB_NAME"
    # FIX-H264-FETCH-HARDENING: https only (also on redirects), --fail, TLS verification kept
    # (never -k/--insecure), and curl's stderr is VISIBLE (-sS: no progress spam, errors on)
    # so a DNS/TLS/HTTP cause reaches the container's stderr.
    if ! curl -fsS --proto '=https' --proto-redir '=https' --max-time 120 -o "$TMP.bz2" "$URL"; then
        rm -f "$TMP.bz2"
        fail "could not download $URL and no cached or mounted library (H264_OPENH264_FILE unset)"
    fi
    from_bz2 "$TMP.bz2" 1
    rm -f "$TMP.bz2"
fi

# exec replaces this shell, so the EXIT trap would not run on the success path: remove the
# (now empty) private temp dir explicitly here; the trap covers every failure/kill path.
rm -rf "$WORK_DIR"

[ "$#" -gt 0 ] || exit 0
exec "$@"
