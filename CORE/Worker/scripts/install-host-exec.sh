#!/usr/bin/env bash
# install-host-exec.sh — standalone installer for the mod-host-exec host helper
# (opsx worker-host-exec, hx.9). The lead calls this from CORE/Worker/scripts/install.sh
# / update.sh in ONE line; this job does NOT edit those (lead-only files).
#
# What it does (every path it writes is under --prefix, default /):
#   1. extracts /usr/local/bin/mod-host-exec out of the worker IMAGE with
#      docker create --pull=never + docker cp + docker rm (never docker pull, never
#      docker run: a missing image is an error, NOT an implicit download).
#   2. creates <prefix>/etc/mod-worker/host-exec.key ONLY if absent: 32 random
#      bytes straight from /dev/urandom, mode 0600, NEVER printed, NEVER
#      overwritten, never copied off-host. An existing key that carries group or
#      world bits, is not exactly 32 bytes, or (on a real root install) is not
#      owned by root is REFUSED (no silent chmod, no regeneration — regenerating
#      would invalidate signatures the API already issued for this install).
#   3. writes <prefix>/etc/mod-worker/host-exec.env (0600) with
#      HOST_EXEC_WORKER_ID_FILE / HOST_EXEC_KEY_FILE / HOST_EXEC_GROUP.
#   4. installs <prefix>/etc/systemd/system/mod-host-exec.service (0644 root:root,
#      via install -m 0644 -o root -g root) from the shipped CORE/Worker/scripts/
#      mod-host-exec.service, whose ExecStart already uses
#      --worker-id-file=${HOST_EXEC_WORKER_ID_FILE}: the worker_id is API-assigned
#      AFTER install (hx.9), so the helper must re-read it per request and fail
#      closed ("worker_id not bound") until the file exists. The legacy
#      HOST_EXEC_WORKER_ID form is documented in the unit header but this unit no
#      longer uses it. The unit is installed VERBATIM: this installer never rewrites
#      or vets its ExecStart, so a shipped unit that still pins the id at start time
#      (--worker-id=) is the lead's to fix, not this installer's to paper over.
#   5. systemctl daemon-reload + enable --now mod-host-exec — ONLY when --prefix
#      is exactly / and --dry-run was not given (anything else is a staging run).
#      A real install (prefix /) additionally requires EUID 0: run as any other uid
#      and it refuses before writing a single byte (a non-root /etc/systemd unit is
#      not a unit systemd will load root:root anyway).
#   6. prints the bind-mount snippet the worker container needs; it never edits a
#      compose file (the lead wires that).
#
# Idempotent: a second run with the same args changes nothing and exits 0.
# Fail closed: unknown flag -> usage + exit 2; --prefix must be an absolute path
# whose characters are [A-Za-z0-9._/-] only (no space, newline, $, backslash — they
# would flow into the env file through the KEY_PATH line) and must not contain '..';
# --prefix itself, the directories under it and every existing target (binary/key/env/
# unit) must NOT be a symlink, so a planted symlink cannot write outside the prefix.
# Missing docker / missing image / empty extracted binary -> non-zero BEFORE anything
# is written to systemd. The unit is installed VERBATIM from
# CORE/Worker/scripts/mod-host-exec.service, so the --worker-id-file= form lives in
# the unit file itself (this job changed it); a shipped unit that still pins the id
# at start time is the lead's to fix, not this installer's to paper over. set -euo
# pipefail, umask 077 while the key + env are written, every temp file comes from
# mktemp in the SAME directory as its target and is removed by an EXIT trap, no eval,
# no unquoted expansion, --group / --image / --prefix validated before any use.
#
# The three env vars above are host-installer inputs (documented here), not a
# new env-var switch for platform behaviour: the helper reads its socket/key/id
# paths from argv only (CORE/Worker/cmd/host-exec-helper/main.go parseFlags).

set -euo pipefail

usage() {
    printf 'usage: install-host-exec.sh --image <worker-image-ref> [--group <name>] [--prefix <dir>] [--dry-run]\n'
    printf '  --image  required; docker image ref that ships /usr/local/bin/mod-host-exec\n'
    printf '  --group  socket-dir group allowed to connect (default mod-worker)\n'
    printf '  --prefix staging root for all written paths (default /)\n'
    printf '  --dry-run  validate + print the plan, write nothing\n'
}

IMAGE=""
GROUP="mod-worker"
PREFIX="/"
DRY=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --image)  [ "$#" -ge 2 ] || { usage; exit 2; }; IMAGE="$2"; shift 2 ;;
        --group)  [ "$#" -ge 2 ] || { usage; exit 2; }; GROUP="$2"; shift 2 ;;
        --prefix) [ "$#" -ge 2 ] || { usage; exit 2; }; PREFIX="$2"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

[ -n "$IMAGE" ] || { usage; exit 2; }
[[ "$IMAGE" =~ ^[A-Za-z0-9][A-Za-z0-9._:/.-]{0,127}$ ]] || { echo "error: --image is not a docker image ref: $IMAGE"; exit 2; }
[[ "$GROUP" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || { echo "error: --group is not a valid group name: $GROUP"; exit 2; }
case "$PREFIX" in
    /*) ;;
    *) echo "error: --prefix must be an absolute path: $PREFIX"; exit 2 ;;
esac
[[ "$PREFIX" =~ ^/[A-Za-z0-9._/-]*$ ]] || { echo "error: --prefix contains a character outside [A-Za-z0-9._/-] (space/newline/\$ would flow into the env file): $PREFIX"; exit 2; }
case "$PREFIX" in
    *..*) echo "error: --prefix must not contain '..': $PREFIX"; exit 2 ;;
esac
PREFIX="${PREFIX%/}"
[ -n "$PREFIX" ] || PREFIX="/"
[ ! -L "$PREFIX" ] || { echo "error: --prefix $PREFIX is a symlink — refusing (every write would escape the prefix)"; exit 2; }

# F4: no component the installer writes into may be a planted symlink. Checked
# before each mkdir/write pair, so a symlinked dir under --prefix can never steer
# a write (key, env, unit) to /etc or anywhere else.
refuse_symlink() {
    local _p
    for _p in "$@"; do
        if [ -L "$_p" ]; then
            echo "error: $PREFIX target $_p is a symlink — refusing (writes would escape the prefix)"
            exit 2
        fi
    done
}

BIN_PATH="$PREFIX/opt/mod-worker/mod-host-exec"
KEY_PATH="$PREFIX/etc/mod-worker/host-exec.key"
ENV_PATH="$PREFIX/etc/mod-worker/host-exec.env"
UNIT_PATH="$PREFIX/etc/systemd/system/mod-host-exec.service"
UNIT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/mod-host-exec.service"
ID_FILE="/var/lib/mod-worker/host-exec/worker_id"
EXTRACT_NAME="mod-host-exec-extract"

if [ "$DRY" -eq 1 ]; then
    printf 'dry-run: would extract %s from %s -> %s (0755), create %s (0600, only if absent), write %s (0600), install %s (0644 root:root)%s\n' \
        "$IMAGE" "$IMAGE" "$BIN_PATH" "$KEY_PATH" "$ENV_PATH" "$UNIT_PATH"
    printf 'dry-run: no systemctl (nothing is enabled on a dry run)\n'
    exit 0
fi

# F5: every temp file is mktemp'd in the SAME directory as its target and is
# removed by this trap, so a failed run leaves no *.new residue behind.
TMP_BIN=""
TMP_KEY=""
TMP_ENV=""
cleanup() {
    docker rm -f "$EXTRACT_NAME" >/dev/null 2>&1 || true
    if [ -n "$TMP_BIN" ] && [ -e "$TMP_BIN" ]; then rm -f "$TMP_BIN"; fi
    if [ -n "$TMP_KEY" ] && [ -e "$TMP_KEY" ]; then rm -f "$TMP_KEY"; fi
    if [ -n "$TMP_ENV" ] && [ -e "$TMP_ENV" ]; then rm -f "$TMP_ENV"; fi
}
trap cleanup EXIT

# F7: a real install (prefix /) is a root install. Refuse before the first byte.
if [ "$PREFIX" = "/" ] && [ "$(id -u)" -ne 0 ]; then
    echo "error: a real install (prefix /) must run as root (EUID 0) — refusing to write /etc + systemd as uid $(id -u)"
    exit 1
fi

command -v docker >/dev/null 2>&1 || { echo "error: docker not found — cannot extract the helper"; exit 1; }
[ -f "$UNIT_SRC" ] || { echo "error: shipped unit $UNIT_SRC not found"; exit 1; }

# --- 1) helper binary from the image ---------------------------------------
HID=""
if HID="$(docker create --pull=never --name "$EXTRACT_NAME" "$IMAGE" 2>/dev/null)"; then :; else HID=""; fi
if [ -z "$HID" ]; then
    echo "error: docker create --pull=never from $IMAGE failed — image missing or unreadable; nothing installed"
    exit 1
fi

refuse_symlink "$PREFIX/opt" "$PREFIX/opt/mod-worker" "$BIN_PATH"
mkdir -p "$PREFIX/opt/mod-worker"
refuse_symlink "$PREFIX/opt/mod-worker"
TMP_BIN="$(mktemp "$PREFIX/opt/mod-worker/mod-host-exec.new.XXXXXX")"
if docker cp "$HID:/usr/local/bin/mod-host-exec" "$TMP_BIN" 2>/dev/null; then :; else
    echo "error: docker cp of /usr/local/bin/mod-host-exec from $IMAGE failed — nothing installed"
    exit 1
fi
if [ ! -s "$TMP_BIN" ]; then
    rm -f "$TMP_BIN"
    TMP_BIN=""
    echo "error: extracted mod-host-exec is empty — refusing to install"
    exit 1
fi
if [ -f "$BIN_PATH" ] && cmp -s "$TMP_BIN" "$BIN_PATH"; then
    rm -f "$TMP_BIN"
    TMP_BIN=""
    echo "mod-host-exec binary unchanged at $BIN_PATH"
else
    chmod 755 "$TMP_BIN"
    mv "$TMP_BIN" "$BIN_PATH"
    TMP_BIN=""
    echo "installed mod-host-exec at $BIN_PATH (0755)"
fi

# --- 2) signing key: create ONLY if absent, never overwrite, never print ----
refuse_symlink "$PREFIX/etc" "$PREFIX/etc/mod-worker" "$KEY_PATH"
mkdir -p "$PREFIX/etc/mod-worker"
refuse_symlink "$PREFIX/etc/mod-worker"
chmod 700 "$PREFIX/etc/mod-worker"
if [ -f "$KEY_PATH" ]; then
    PERM="$(stat -c %a "$KEY_PATH")"
    KEY_SIZE="$(stat -c %s "$KEY_PATH")"
    KEY_OWNER="$(stat -c %U "$KEY_PATH")"
    if [ "$(( 8#$PERM & 8#077 ))" -ne 0 ]; then
        echo "error: existing key $KEY_PATH has mode $PERM (group/world readable) — refusing; not chmod'ing, not regenerating"
        exit 3
    fi
    if [ "$KEY_SIZE" -ne 32 ]; then
        echo "error: existing key $KEY_PATH is $KEY_SIZE bytes, not 32 — refusing; not regenerating, not truncating"
        exit 3
    fi
    if [ "$(id -u)" -eq 0 ] && [ "$PREFIX" = "/" ] && [ "$KEY_OWNER" != root ]; then
        echo "error: existing key $KEY_PATH is owned by $KEY_OWNER, not root — refusing"
        exit 3
    fi
    echo "kept existing host-exec signing key at $KEY_PATH"
else
    TMP_KEY="$(mktemp "$PREFIX/etc/mod-worker/host-exec.key.new.XXXXXX")"
    (
        umask 077
        head -c 32 /dev/urandom > "$TMP_KEY"
        chmod 600 "$TMP_KEY"
        mv "$TMP_KEY" "$KEY_PATH"
    )
    TMP_KEY=""
    echo "generated host-exec signing key at $KEY_PATH (0600, 32 random bytes, never printed)"
fi

# --- 3) env file (0600, installer inputs, no secret VALUE in it) ------------
refuse_symlink "$ENV_PATH"
TMP_ENV="$(mktemp "$PREFIX/etc/mod-worker/host-exec.env.new.XXXXXX")"
(
    umask 077
    printf 'HOST_EXEC_WORKER_ID_FILE=%s\nHOST_EXEC_KEY_FILE=%s\nHOST_EXEC_GROUP=%s\n' \
        "$ID_FILE" "$KEY_PATH" "$GROUP" > "$TMP_ENV"
    chmod 600 "$TMP_ENV"
    mv "$TMP_ENV" "$ENV_PATH"
)
TMP_ENV=""
echo "wrote $ENV_PATH (0600)"

# --- 4) unit ---------------------------------------------------------------
refuse_symlink "$PREFIX/etc/systemd" "$PREFIX/etc/systemd/system" "$UNIT_PATH"
mkdir -p "$PREFIX/etc/systemd/system"
refuse_symlink "$PREFIX/etc/systemd/system"
if [ -f "$UNIT_PATH" ] && cmp -s "$UNIT_SRC" "$UNIT_PATH"; then
    echo "systemd unit unchanged at $UNIT_PATH"
else
    # F3: the unit must land root:root 0644 even when the repo checkout is owned
    # by a non-root user — cp -p would have preserved that owner and left a
    # root-run unit user-writable. install sets owner/group/mode explicitly.
    if [ "$(id -u)" -eq 0 ]; then
        install -m 0644 -o root -g root "$UNIT_SRC" "$UNIT_PATH"
    else
        install -m 0644 "$UNIT_SRC" "$UNIT_PATH"
    fi
    echo "installed systemd unit at $UNIT_PATH (0644)"
fi

# --- 5) systemd: only a real install (prefix / and not dry) ----------------
if [ "$PREFIX" = "/" ]; then
    if command -v systemctl >/dev/null 2>&1; then
        systemctl daemon-reload || { echo "error: systemctl daemon-reload failed"; exit 1; }
        systemctl enable --now mod-host-exec || { echo "error: systemctl enable --now mod-host-exec failed"; exit 1; }
        echo "mod-host-exec enabled and started"
    else
        echo "error: systemctl not found — unit installed but not enabled"
        exit 1
    fi
else
    echo "staging prefix ($PREFIX): systemctl NOT run — unit + files staged only"
fi

# --- 6) the bind mounts the worker container needs (compose is the lead's) --
echo "worker container bind mounts (add to the compose volume list for my_mod_worker):"
echo "  -v /run/mod-host-exec:/run/mod-host-exec -v /etc/mod-worker/host-exec.key:/etc/mod-worker/host-exec.key:ro -v /var/lib/mod-worker/host-exec:/var/lib/mod-worker/host-exec"
exit 0
