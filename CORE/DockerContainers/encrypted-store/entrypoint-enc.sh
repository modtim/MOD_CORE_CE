#!/bin/sh
# CORE/DockerContainers/encrypted-store/entrypoint-enc.sh - ENC-IMAGES (owner 11-10:
# "redesign it").
#
# ONE entrypoint for BOTH in-container encrypted stores (versitygw-enc, valkey-enc).
# The store mounts gocryptfs over ITS OWN data dir INSIDE the same container, so the
# compose needs NO `:rshared`/`:rslave` bind propagation - which is exactly what
# Docker Desktop / WSL2 cannot do (results-1110/SIDECAR-WSL2-PROBE.md: "path ... is
# not a shared mount"). A mount that lives inside the container needs no propagation.
#
# Lifecycle: passfile guard -> init ONCE over a virgin cipher dir -> mount gocryptfs
# (root, FOREGROUND, backgrounded so PID 1 supervises it) at /data -> bounded wait
# until `mountpoint -q /data` -> run the store as its NON-ROOT user with the whole
# capability set dropped -> forward TERM/INT to the store, WAIT FOR IT TO REALLY
# EXIT, then unmount.
#
# PID 1 handling: a TRAP LOOP in this shell (chosen over tini: tini would be a 4th
# pinned package for no extra guarantee - this shell is PID 1). Two rules the cloud
# security review (FIX-ENTRYPOINT) made explicit:
#  1. `wait` returning after a trapped signal does NOT mean the child is gone
#     (POSIX: wait returns >128 immediately). Exiting there would let PID 1 go and
#     SIGKILL a store that is still flushing (Valkey final RDB save, versitygw
#     writes), so PID 1 loops until `kill -0` says the store is gone, keeps the
#     store's real exit code, and only THEN unmounts.
#  2. PID 1 supervises BOTH the store and the FUSE daemon: a dead mount must exit
#     the container so `restart: unless-stopped` brings it back. A store TERM'd
#     over a DEAD mount cannot finish its final save and refuses to exit at all
#     (Valkey: "# Error trying to save the DB, can't exit." - proven on cpu4,
#     ENC-PROOF2 claim G), so the fuse-dead path BOUNDS that wait
#     (ENCSTORE_FUSE_DEAD_GRACE, default 8 s) and escalates to KILL; the normal
#     docker-stop path keeps its UNBOUNDED wait for the store's final flush.
#
# NEVER print the passphrase or the passfile contents: only paths are logged.

set -eu

die() {
    printf 'encstore: %s\n' "$*" >&2
    exit 1
}

# --- shutdown helper: retry, then lazy, and say which one worked -------------
# The store is gone before this runs, but FUSE can still be BUSY for a moment
# while it finishes flushing, so one `fusermount3 -u` fails; it used to be
# swallowed by `|| true` and left a stale mount under the host bind.
unmount_data() {
    # FIX-ENTRYPOINT-3 (ENC-PROOF2-local claim F, the Dell): the store's cwd is
    # INSIDE the mount (the `cd "$DATA_DIR"` below is what makes Valkey write into
    # the view), so PID 1's own shell cwd is inside /data too - and a mount cannot
    # be unmounted NON-LAZILY while any process has its cwd in it. Without leaving
    # it first, `fusermount3 -u` fails "Resource busy" every time, the retry loop
    # burns ~5 s of the 5 s compose stop grace and PID 1 is SIGKILLed (ExitCode
    # 137). The store is already gone here (its own cwd with it), so step out of
    # /data before ANY unmount attempt. The retry loop below stays: it is still
    # right for some OTHER process holding the mount.
    cd /
    tries=0
    while [ "$tries" -lt 5 ]; do
        if fusermount3 -u "$DATA_DIR" 2>/dev/null; then
            printf 'encstore: unmounted %s (fusermount3 -u)\n' "$DATA_DIR"
            return 0
        fi
        tries=$((tries + 1))
        sleep 1
    done
    # last resort: lazy unmount works on a busy or already-dead mount.
    if fusermount3 -uz "$DATA_DIR" 2>/dev/null; then
        printf 'encstore: unmounted %s lazily (fusermount3 -uz)\n' "$DATA_DIR"
        return 0
    fi
    umount "$DATA_DIR" 2>/dev/null || true
    printf 'encstore: %s NOT unmounted - a stale FUSE mount may survive under the bind\n' "$DATA_DIR" >&2
    return 0
}

rc=0

# --- which store am I? (set by the image, never by a secret) -----------------
STORE="${MCW_ENCSTORE:-}"
STORE_BIN=""
STORE_UID=""
STORE_GID=""
case "$STORE" in
    versitygw) STORE_BIN=/usr/local/bin/versitygw;   STORE_UID=1000; STORE_GID=1000 ;;
    valkey)    STORE_BIN=/usr/local/bin/valkey-server; STORE_UID=999; STORE_GID=1000 ;;
    *) die "MCW_ENCSTORE is not versitygw|valkey - refusing to guess the store" ;;
esac

CIPHER_DIR=/ciphertext-raw
DATA_DIR=/data
PASS_FILE=/run/secrets/gocryptfs-pass
# the fuse-dead flag lives in a root-only dir (never in world-writable /tmp: the
# store uid could plant a symlink there). It is created and cleared before the
# watcher starts (see below).
FUSE_DEAD_DIR=/run/encstore
FUSE_DEAD_FLAG="$FUSE_DEAD_DIR/fuse-dead"
# valkey's requirepass config lives on a tmpfs (/dev/shm), never on the data volume,
# never in an image layer (see the BEGIN/END valkey-conf block below).
VALKEY_CONF_DIR=/dev/shm/encstore-valkey

# --- preconditions (fail closed; nothing printed beyond paths) ---------------
[ -d "$CIPHER_DIR" ] || die "cipher dir $CIPHER_DIR is not a directory (host bind missing?)"
[ -d "$DATA_DIR" ]   || die "data dir $DATA_DIR is not a directory (image bug?)"
[ -f "$PASS_FILE" ]  || die "passfile $PASS_FILE is not a file - no key material, refusing to mount"
[ -s "$PASS_FILE" ]  || die "passfile $PASS_FILE is empty - refusing to mount or to init"

# --- init EXACTLY ONCE, and only over a virgin cipher dir -------------------
if [ ! -f "$CIPHER_DIR/gocryptfs.conf" ]; then
    if [ -n "$(find "$CIPHER_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
        die "refusing to -init: $CIPHER_DIR already holds entries but has no gocryptfs.conf - this is existing ciphertext (or a bad bind); re-mount with the original passphrase, never re-init over it"
    fi
    # -q: quiet; the passphrase comes from the file only, never argv, never echoed.
    gocryptfs -init -q -passfile="$PASS_FILE" "$CIPHER_DIR"
    [ -f "$CIPHER_DIR/gocryptfs.conf" ] || die "init did not produce $CIPHER_DIR/gocryptfs.conf"
fi

# gocryptfs insists the mountpoint is empty; leftovers = an unclean previous unmount.
if [ -n "$(find "$DATA_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
    die "mount point $DATA_DIR is not empty - stale leftovers from an unclean unmount (or a store that wrote into the container layer); recover with docker compose rm <service> (that discards the container's writable layer), NEVER delete or edit $CIPHER_DIR"
fi

# --- ownership prep (root, BEFORE the mount) --------------------------------
# The host creates the cipher dir 0700 root-owned (CE default). gocryptfs maps the
# cipher dir's owner+mode onto the /data view, so a root-owned dir would leave the
# store uid unable to write its own view. Fix it HERE, while this shell is still
# root: chown the cipher dir ROOT ENTRY ONLY - never recursive, it can hold
# terabytes, and the FUSE daemon runs as root anyway - and keep it 0700.
chown "$STORE_UID:$STORE_GID" "$CIPHER_DIR" 2>/dev/null || \
    printf 'encstore: could not chown %s to %s:%s - continuing, the writability guard below still fails closed (a host filesystem that ignores chown, e.g. a Windows bind, lands here)\n' "$CIPHER_DIR" "$STORE_UID" "$STORE_GID" >&2
chmod 0700 "$CIPHER_DIR" 2>/dev/null || \
    printf 'encstore: could not chmod %s to 0700 - continuing, the guard below still fails closed\n' "$CIPHER_DIR" >&2
printf 'encstore: cipher dir %s prepared for the store uid (owner %s:%s, mode 0700)\n' "$CIPHER_DIR" "$STORE_UID" "$STORE_GID"

# versitygw keeps a small state dir (/iam) that the compose hands it as a NAMED
# VOLUME - Docker creates that volume root-owned, so uid 1000 used to fail with
# "open /iam/users.json: permission denied". It is tiny, so recursive chown is
# fine here (never on the cipher dir).
if [ "$STORE" = versitygw ]; then
    # -h: never follow symlinks (the store uid owns and writes /iam, so a
    # compromised store could plant one pointing at a root-owned path). Only run
    # when the top-level owner differs from the store uid: after the first start
    # there is nothing to fix and no reason to walk store-controlled content as root.
    if [ "$(stat -c %u /iam 2>/dev/null || echo unknown)" != "$STORE_UID" ]; then
        chown -R -h "$STORE_UID:$STORE_GID" /iam 2>/dev/null || \
            printf 'encstore: could not chown /iam to the store uid - versitygw will fail closed on its state file\n' >&2
    fi
fi

# --- grace for the fuse-dead shutdown (env-overridable, validated) ----------
# A store TERM'd while /data is a DEAD mount cannot write its final save and
# REFUSES to exit at all (cpu4, ENC-PROOF2 claim G: Valkey logs "# Error trying
# to save the DB, can't exit." and keeps running), so PID 1's unbounded wait
# below would hang forever: the container stays `running`, ExitCode 0,
# RestartCount 0, health unhealthy, and `restart: unless-stopped` never fires.
# The fuse-dead path waits at most this many seconds, then KILLs (nothing can be
# saved over a dead mount anyway). This is the ONLY bounded wait: a plain `docker
# stop` on a HEALTHY mount keeps the unbounded wait below, so the store's final
# flush is allowed to finish (docker's own stop timeout is the limit there).
FUSE_DEAD_GRACE="${ENCSTORE_FUSE_DEAD_GRACE:-8}"
case "$FUSE_DEAD_GRACE" in
    ''|*[!0-9]*) die "ENCSTORE_FUSE_DEAD_GRACE must be an integer number of seconds (got: $FUSE_DEAD_GRACE)" ;;
esac

# --- PID 1 signal handling, installed BEFORE the mount wait ------------------
# A `docker stop` during startup must exit in seconds, not sit in the bounded
# mount wait until the 10 s SIGKILL that follows the TERM.
STORE_PID=0
TERM_SEEN=0
on_signal() {
    TERM_SEEN=1
    if [ "$STORE_PID" -gt 0 ]; then
        kill -TERM "$STORE_PID" 2>/dev/null || true
    fi
    return 0
}
trap on_signal TERM INT

# --- valkey: requirepass via a 0400 config file, never argv ------------------
# valkey has no env or file form for requirepass, and `--requirepass <pw>` on the
# command line is visible in `docker inspect` and `ps`. So build a config file from
# the REDIS_PASSWORD env var (the compose passes it as environment, not command),
# on a tmpfs, owned by the store uid, mode 0400, written under umask 077 with
# printf (a shell builtin: the password never reaches any process argv, no set -x),
# and hand it to valkey as the FIRST argument (valkey requires the config file first).
# Empty or unset password: refuse to start - never run unauthenticated.
# BEGIN valkey-conf-write
if [ "$STORE" = valkey ]; then
    [ -n "${REDIS_PASSWORD:-}" ] || die "REDIS_PASSWORD is empty or unset - refusing to start valkey unauthenticated"
    case "$REDIS_PASSWORD" in
        *"
"*) die "REDIS_PASSWORD contains a newline - cannot be written to a valkey config line" ;;
    esac
    old_umask=$(umask)
    umask 077
    mkdir -p -m 0700 "$VALKEY_CONF_DIR" || die "cannot create $VALKEY_CONF_DIR; refusing to start valkey"
    rm -f "$VALKEY_CONF_DIR/valkey.conf" || die "cannot clear the old valkey config; refusing to start valkey"
    # escape \ and " for valkey's double-quoted config string (sed reads stdin: no argv exposure)
    esc=$(printf '%s' "$REDIS_PASSWORD" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')
    printf 'requirepass "%s"\n' "$esc" > "$VALKEY_CONF_DIR/valkey.conf" || die "cannot write the valkey config; refusing to start valkey"
    esc=""
    umask "$old_umask"
    chown "$STORE_UID:$STORE_GID" "$VALKEY_CONF_DIR" "$VALKEY_CONF_DIR/valkey.conf" || die "cannot chown the valkey config to the store user"
    chmod 0700 "$VALKEY_CONF_DIR" || die "cannot chmod $VALKEY_CONF_DIR to 0700"
    chmod 0400 "$VALKEY_CONF_DIR/valkey.conf" || die "cannot chmod the valkey config to 0400"
fi
# END valkey-conf-write

# --- mount (root, foreground, backgrounded: PID 1 watches it) ---------------
# -fg + `&`: the FUSE daemon stays a CHILD of PID 1, so the supervision below
# can see it die. A daemonized mount reparents it and PID 1 never notices.
# -allow_other so the store's non-root uid can traverse the view; gocryptfs
# enforces the view's own permissions from the cipher dir's modes - the ownership
# prep above chowns the cipher dir root entry to the store uid (mode 0700), which
# is what makes the view writable by a uid-1000 store without a 0777 host dir.
gocryptfs -q -passfile="$PASS_FILE" -allow_other -fg "$CIPHER_DIR" "$DATA_DIR" &
FUSE_PID=$!

# Bounded wait: fail closed rather than start a store on a directory that is NOT
# the decrypted view (that would write PLAINTEXT into the cipher dir).
i=0
while [ "$i" -lt 60 ]; do
    if mountpoint -q "$DATA_DIR"; then break; fi
    if ! kill -0 "$FUSE_PID" 2>/dev/null; then break; fi
    if [ "$TERM_SEEN" -eq 1 ]; then break; fi
    sleep 0.5
    i=$((i + 1))
done
# stopped during startup: exit promptly (a `docker stop` that lands here must
# not sit in the bounded wait until the 10 s SIGKILL), and never start a store
# that is already being stopped.
if [ "$TERM_SEEN" -eq 1 ]; then
    printf 'encstore: stopped during startup - not starting the store\n' >&2
    if mountpoint -q "$DATA_DIR"; then unmount_data; fi
    exit 0
fi
mountpoint -q "$DATA_DIR" || die "gocryptfs mount never appeared at $DATA_DIR - refusing to start the store"

# --- the store's cwd MUST be the mounted view --------------------------------
# Proven on xeon (ENC-PROOF-LINUX): without a cd, the store's cwd stays the
# underlying UNMOUNTED image dir /data, so Valkey's SET + SAVE returned OK while
# dump.rdb was created in the container's WRITABLE LAYER (`docker diff` showed
# "A /data/dump.rdb", plaintext marker in the overlay snapshot) and NOTHING
# landed in the cipher dir. Enter the view, then fail closed if we are not in it.
cd "$DATA_DIR" || die "cannot enter the mounted view $DATA_DIR - refusing to start the store outside it"

# runtime self-check: /data must really be a mount, and we must be inside it.
mountpoint -q "$DATA_DIR" || die "$DATA_DIR stopped being a mount before the store started - refusing to run"
dev_data=$(stat -c %d "$DATA_DIR" 2>/dev/null || true)
dev_root=$(stat -c %d / 2>/dev/null || true)
if [ -n "$dev_data" ] && [ -n "$dev_root" ]; then
    [ "$dev_data" != "$dev_root" ] || die "/data is on the SAME device as / - it is not a mount; the store would write PLAINTEXT into the container layer"
fi
case "$(pwd -P)" in
    "$DATA_DIR"|"$DATA_DIR"/*) ;;
    *) die "the store's cwd ($(pwd -P)) is not inside the mounted view $DATA_DIR - refusing to write outside it" ;;
esac

# --- privilege check for the store user (fail closed before any store byte) --
setpriv --reuid="$STORE_UID" --regid="$STORE_GID" --clear-groups sh -c \
    'test -w /data' || die "the store user cannot write $DATA_DIR - the ownership prep above (chown of the cipher dir root entry to the store uid, mode 0700) did not take effect: the host filesystem ignores chown (a Windows/WSL bind lands here) or the view is not writable; refusing to start the store"

# the compose's `command:` may name the program first (`valkey-server ...`); drop it.
case "${1:-}" in
    versitygw) shift ;;
    valkey-server)
        shift
        # valkey writes dump.rdb in its CWD: pin the data dir explicitly as well,
        # so a cwd slip can never write plaintext outside the mount (the compose
        # command is lead-owned; adding it here keeps the store correct either way
        # and is skipped if the compose already passed --dir).
        has_dir=0
        for a in "$@"; do
            case "$a" in
                --dir|--dir=*) has_dir=1 ;;
            esac
        done
        if [ "$has_dir" -eq 0 ]; then
            set -- "$@" --dir "$DATA_DIR"
        fi
        ;;
esac

# valkey takes its config file as the FIRST argument (written above, before the mount).
# BEGIN valkey-conf-arg
if [ "$STORE" = valkey ]; then
    set -- "$VALKEY_CONF_DIR/valkey.conf" "$@"
fi
# END valkey-conf-arg

# root-only flag dir, flag cleared before the store and the watcher exist (no
# stale flag from an earlier cycle, no symlink planting by the store uid). Done
# BEFORE the store starts and fatal on failure: under set -e a failure after the
# store start would orphan a running store.
mkdir -p -m 0700 "$FUSE_DEAD_DIR" || die "cannot create the root-only flag dir $FUSE_DEAD_DIR; refusing to start the store"
chmod 0700 "$FUSE_DEAD_DIR" || die "cannot chmod $FUSE_DEAD_DIR to 0700; refusing to start the store"
rm -f "$FUSE_DEAD_FLAG" || die "cannot clear the stale flag $FUSE_DEAD_FLAG; refusing to start the store"

# --- run the store UNPRIVILEGED ---------------------------------------------
# The compose grants CAP_SYS_ADMIN for the mount; the network-facing store must
# not keep any of it. util-linux `setpriv` (installed from the pinned apk, and
# first on PATH: /usr/bin/setpriv in versitygw-enc, /bin/setpriv in valkey-enc) clears the bounding set, the inheritable
# and the ambient sets, and refuses setuid/setgid + file-capability execs.
STORE_PID=0
setpriv --reuid="$STORE_UID" --regid="$STORE_GID" --clear-groups \
        --bounding-set=-all --inh-caps=-all --ambient-caps=-all --no-new-privs \
        "$STORE_BIN" "$@" &
STORE_PID=$!
# a TERM that landed between the TERM_SEEN check above and this assignment saw
# STORE_PID=0 and was lost: deliver it now.
if [ "$TERM_SEEN" -eq 1 ]; then
    kill -TERM "$STORE_PID" 2>/dev/null || true
fi

# --- FUSE daemon liveness (the fuse-dead path) ------------------------------
# busybox ash has no `wait -n`, so the FUSE daemon gets its own watcher subshell:
# if it dies, /data is a dead mount and the store would serve from nothing. TERM
# the store, mark the container as failed, then FORCE the store out: a store on a
# dead mount cannot write its final save and may refuse to exit at all (cpu4,
# ENC-PROOF2 claim G: Valkey logs "# Error trying to save the DB, can't exit."
# and keeps running, so PID 1's wait below would hang forever and the container
# would never restart). Wait at most ENCSTORE_FUSE_DEAD_GRACE seconds (poll
# `kill -0` once per second), then KILL - nothing can be saved over a dead mount,
# so the final save was already impossible and anything changed since the last
# successful save is LOST. The main flow then reaps it with `wait`, lazy-unmounts
# and exits 1. A TERM that arrives during this handling only re-runs the trap
# (a second TERM on a store that is already being forced out); this is not a
# loop, so it cannot re-enter or hang.
(
    while kill -0 "$FUSE_PID" 2>/dev/null; do
        kill -0 "$STORE_PID" 2>/dev/null || exit 0
        sleep 1
    done
    printf 'encstore: the FUSE daemon (pid %s) exited - /data is a dead mount; stopping the container\n' "$FUSE_PID" >&2
    # flag FIRST, then TERM: a store that dies instantly must never let the main
    # loop test the flag before it exists.
    # `|| true`: under set -e a failed write must not abort the watcher before the
    # TERM below (that would reintroduce the hang on a dead mount).
    printf 'dead\n' > "$FUSE_DEAD_FLAG" || true
    kill -TERM "$STORE_PID" 2>/dev/null || true
    waited=0
    while [ "$waited" -lt "$FUSE_DEAD_GRACE" ]; do
        kill -0 "$STORE_PID" 2>/dev/null || exit 0
        waited=$((waited + 1))
        sleep 1
    done
    if kill -0 "$STORE_PID" 2>/dev/null; then
        printf 'encstore: the store (pid %s) refused to exit after %s s - sending KILL; its final save was impossible over the dead mount, so anything it changed since the last successful save is LOST\n' "$STORE_PID" "$FUSE_DEAD_GRACE" >&2
        kill -KILL "$STORE_PID" 2>/dev/null || true
    fi
) &

# --- wait for the store to REALLY exit --------------------------------------
# POSIX `wait` returns >128 as soon as a signal is trapped, WITHOUT the child
# being gone: exiting there would let PID 1 go and SIGKILL a store that is still
# flushing (Valkey final RDB save, versitygw writes). Loop until kill -0 says the
# store is gone, and keep the store's real exit code. This wait is UNBOUNDED on
# the normal docker-stop path: docker's own stop timeout is the limit there, and
# the store's final flush over a HEALTHY mount must be allowed to finish. The
# fuse-dead path is bounded in the watcher above (TERM, grace, KILL), so this
# loop can never hang on a dead mount.
while :; do
    if wait "$STORE_PID"; then rc=0; else rc=$?; fi
    kill -0 "$STORE_PID" 2>/dev/null || break
done

# --- exit status ------------------------------------------------------------
# a dead mount is a failure the supervisor must act on; a plain `docker stop`
# (TERM/INT forwarded) is a clean shutdown.
if [ -f "$FUSE_DEAD_FLAG" ]; then
    # the fuse-dead path only: reap the store (the watcher already TERMed and,
    # after the grace, KILLed it), lazy-unmount best effort (an eager unmount
    # cannot work over a dead mount), then exit 1 so `restart: unless-stopped`
    # brings the store back. A TERM arriving during this handling re-runs the
    # trap (a no-op second TERM on a store that is already gone) - this branch
    # is not a loop, so it cannot re-enter or hang.
    printf 'encstore: the mount died - the store was forced out; reaping it and exiting 1 so restart: unless-stopped brings it back\n' >&2
    if wait "$STORE_PID"; then rc=0; else rc=$?; fi
    rc=1
    if mountpoint -q "$DATA_DIR"; then
        # same FIX-ENTRYPOINT-3 reason: PID 1's cwd is inside the mount - leave it
        # before unmounting (lazy would work anyway, but the cwd is the bug class).
        cd /
        fusermount3 -uz "$DATA_DIR" 2>/dev/null || \
            printf 'encstore: could not lazy-unmount %s - the container layer still holds the mount point\n' "$DATA_DIR" >&2
    fi
    exit 1
elif [ "$TERM_SEEN" -eq 1 ]; then
    rc=0
fi

# --- clean shutdown: unmount AFTER the store is gone ------------------------
if mountpoint -q "$DATA_DIR"; then
    unmount_data
fi
exit "$rc"
