#!/usr/bin/env bash
# Tests for the binary-selection logic in scripts/entrypoint.sh (BUG.WORKER-STALE-BINARY).
#
# The entrypoint runs the worker from a persistent volume so self-update survives
# restarts, and decides at every boot whether the image binary or the persistent
# one runs. These tests drive the REAL script through fake binaries (shell scripts
# that answer `--version` and otherwise print which build ran), with the two paths
# redirected into a temp dir via MOD_WORKER_IMAGE_BIN / MOD_WORKER_PERSIST_DIR.
#
# Run:  bash scripts/tests/entrypoint_test.sh   (exit 0 = pass)
set -u
ENTRY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/entrypoint.sh"
[ -f "$ENTRY" ] || { echo "FAIL: cannot find $ENTRY"; exit 1; }

pass=0; fail=0
check() { # desc expected actual
    if [ "$2" = "$3" ]; then pass=$((pass+1));
    else fail=$((fail+1)); echo "FAIL: $1 -> expected [$2] got [$3]"; fi
}

# mk <path> <version-string> <marker>: a fake worker binary.
mk() {
    printf '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "mod-worker %s"; exit 0; fi\necho "RAN:%s $*"\n' "$2" "$3" > "$1"
    chmod 755 "$1"
}

# run_case: creates a sandbox, calls $1 to populate it, runs the entrypoint under
# dash-compatible `sh` (the image's /bin/sh), and leaves OUT / SB set.
run_case() {
    SB="$(mktemp -d)"
    mkdir -p "$SB/img" "$SB/vol"
    "$1"
    OUT="$(MOD_WORKER_IMAGE_BIN="$SB/img/mod-worker" MOD_WORKER_PERSIST_DIR="$SB/vol" sh "$ENTRY" --probe 2>&1)"
}

persist_marker() { sh "$SB/vol/mod-worker" 2>/dev/null | sed -e 's/^RAN:\([^ ]*\).*/\1/'; }

# 1. first boot: nothing persistent yet -> the image binary is bootstrapped and runs
c1() { mk "$SB/img/mod-worker" v1.0.84 IMAGE; }
run_case c1
check "first boot runs the image binary" "RAN:IMAGE --probe" "$OUT"
check "first boot bootstraps the volume" "IMAGE" "$(persist_marker)"

# 2. same version, identical bytes: nothing to do
c2() { mk "$SB/img/mod-worker" v1.0.84 IMAGE; cp "$SB/img/mod-worker" "$SB/vol/mod-worker"; }
run_case c2
check "identical binaries keep running" "RAN:IMAGE --probe" "$OUT"

# 3. THE BUG: same version string, but the image was rebuilt (different bytes)
c3() { mk "$SB/img/mod-worker" v1.0.84 NEW; mk "$SB/vol/mod-worker" v1.0.84 STALE; }
run_case c3
check "rebuilt image with an UNBUMPED version wins over the stale volume binary" "RAN:NEW --probe" "$OUT"
check "stale volume binary is replaced by the image binary" "NEW" "$(persist_marker)"

# 4. image has a higher version: image wins (existing behaviour)
c4() { mk "$SB/img/mod-worker" v1.0.90 NEW; mk "$SB/vol/mod-worker" v1.0.84 OLD; }
run_case c4
check "higher image version wins" "RAN:NEW --probe" "$OUT"
check "higher image version is copied into the volume" "NEW" "$(persist_marker)"

# 5. volume has a HIGHER version (a real self-update): the volume binary must keep winning
c5() { mk "$SB/img/mod-worker" v1.0.84 IMAGE; mk "$SB/vol/mod-worker" v1.0.99 SELFUPDATED; }
run_case c5
check "self-updated (higher) volume binary keeps running" "RAN:SELFUPDATED --probe" "$OUT"
check "self-updated volume binary is not overwritten" "SELFUPDATED" "$(persist_marker)"

# 6. unusable volume binary (reports dev): image wins
c6() { mk "$SB/img/mod-worker" v1.0.84 IMAGE; mk "$SB/vol/mod-worker" dev BROKEN; }
run_case c6
check "a dev/unusable volume binary is replaced by the image" "RAN:IMAGE --probe" "$OUT"

# 7. arguments pass through to the chosen binary
c7() { mk "$SB/img/mod-worker" v1.0.84 IMAGE; }
SB="$(mktemp -d)"; mkdir -p "$SB/img" "$SB/vol"; c7
OUT="$(MOD_WORKER_IMAGE_BIN="$SB/img/mod-worker" MOD_WORKER_PERSIST_DIR="$SB/vol" sh "$ENTRY" --a "b c" 2>&1)"
check "arguments are passed through unchanged" "RAN:IMAGE --a b c" "$OUT"

# 9. no image binary at the configured path (a compose override that points at a
# missing path): a WORKING volume binary must keep running and never be clobbered
# by an empty version read.
c9() { mk "$SB/vol/mod-worker" v1.0.84 VOLONLY; }
run_case c9
check "missing image binary keeps the working volume binary" "RAN:VOLONLY --probe" "$OUT"
check "volume binary untouched when the image is unreadable" "VOLONLY" "$(persist_marker)"

# 10. semver ordering across a double-digit bump: sort -V must beat lexicographic
# (v1.0.100 > v1.0.99), otherwise a rolled worker keeps asking for an older build.
c10() { mk "$SB/img/mod-worker" v1.0.100 NEW; mk "$SB/vol/mod-worker" v1.0.99 OLD; }
run_case c10
check "v1.0.100 image beats a v1.0.99 volume binary" "RAN:NEW --probe" "$OUT"
check "v1.0.100 is copied into the volume" "NEW" "$(persist_marker)"

# 11. a REAL ELF is executing at the persisted path while the entrypoint installs
# the rebuilt image over it (DIAG-WORKER-ENTRYPOINT-TEXT-FILE-BUSY): plain `cp` onto
# the busy file dies with ETXTBSY ("Text file busy") under set -e and crash-loops the
# container; a temp-file + rename over it works (the old inode lives on until released).
BUSY_PID=""
c11() { cp /bin/sleep "$SB/vol/mod-worker"; chmod 755 "$SB/vol/mod-worker"
        "$SB/vol/mod-worker" 30 & BUSY_PID=$!
        mk "$SB/img/mod-worker" v9.9.9 NEW; }
run_case c11
check "installing over a busy persisted binary does not crash-loop the container" "RAN:NEW --probe" "$OUT"
check "the busy volume binary is replaced by the image binary" "NEW" "$(persist_marker)"
kill "$BUSY_PID" 2>/dev/null; wait "$BUSY_PID" 2>/dev/null

# big <path> <version> <marker>: a fake worker binary padded to ~200 KB so a
# copy of it is long enough to be interrupted mid-copy / to interleave.
big() { printf '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "mod-worker %s"; exit 0; fi\necho "RAN:%s $*"\n' "$2" "$3" > "$1"
        { printf '#'; head -c 200000 /dev/zero | tr '\0' 'A'; } >> "$1"; chmod 755 "$1"; }

# 12. failed-copy cleanup (REV-SECURITY-WORKER-ENTRYPOINT-ATOMIC-INSTALL item 4):
# (a) an unreadable image binary makes the copy fail; (b) `ulimit -f 1` kills cp
# mid-copy (the OOM/kill/crash era of DIAG-WORKER-ENTRYPOINT-TEXT-FILE-BUSY).
# The helper must remove ONLY its own temp (no residue in the volume dir) and
# leave the live binary unchanged.
# N5: 12a relies on `chmod 000` blocking cp; as root (uid 0) the copy succeeds,
# so the unreadable-source case is skipped when running as root.
if [ "$(id -u)" -ne 0 ]; then
SB12="$(mktemp -d)"; mkdir -p "$SB12/img" "$SB12/vol"
mk "$SB12/img/mod-worker" v1.0.84 IMAGE; chmod 000 "$SB12/img/mod-worker"
OUT12="$(MOD_WORKER_IMAGE_BIN="$SB12/img/mod-worker" MOD_WORKER_PERSIST_DIR="$SB12/vol" sh "$ENTRY" --probe 2>&1)"
rc12=$?
res12="$(find "$SB12/vol" -maxdepth 1 -name '*mod-worker*' ! -name 'mod-worker' | wc -l | tr -d ' ')"
check "unreadable source: no temp residue in the volume dir" "0" "$res12"
check "unreadable source: the live binary is unchanged" "absent" "$([ -x "$SB12/vol/mod-worker" ] && echo present || echo absent)"
check "unreadable source: boot aborts non-zero with a FATAL message" "1:yes" "$rc12:$(printf '%s' "$OUT12" | grep -q FATAL && echo yes || echo no)"
rm -rf "$SB12"
fi

SB12B="$(mktemp -d)"; mkdir -p "$SB12B/img" "$SB12B/vol"
big "$SB12B/img/mod-worker" v1.0.84 IMAGE; mk "$SB12B/vol/mod-worker" v1.0.84 VOL
OUT12B="$( ulimit -f 1; MOD_WORKER_IMAGE_BIN="$SB12B/img/mod-worker" MOD_WORKER_PERSIST_DIR="$SB12B/vol" sh "$ENTRY" --probe 2>&1 )"
rc12b=$?
res12b="$(find "$SB12B/vol" -maxdepth 1 -name '*mod-worker*' ! -name 'mod-worker' | wc -l | tr -d ' ')"
check "mid-copy kill: no temp residue remains in the volume dir" "0" "$res12b"
check "mid-copy kill: the live binary is unchanged" "VOL" "$(sh "$SB12B/vol/mod-worker" 2>/dev/null | sed -e 's/^RAN:\([^ ]*\).*/\1/')"
check "mid-copy kill: boot aborts non-zero (old binary NOT exec'd)" "1" "$rc12b"
rm -rf "$SB12B"

# 13. two-writer collision on the same directory (item 2):
# (a) a decoy symlink at the OLD fixed temp name must never be written through;
# (b) two concurrent installs into one volume dir must land exactly one whole source.
c13a() { printf 'SENTINEL\n' > "$SB/sentinel"; ln -s "$SB/sentinel" "$SB/vol/mod-worker.new"
        mk "$SB/img/mod-worker" v1.0.90 NEW; }
run_case c13a
check "symlink decoy at the old fixed temp name is not followed" "RAN:NEW --probe" "$OUT"
check "the sentinel file is not written through" "SENTINEL" "$(cat "$SB/sentinel" 2>/dev/null)"
check "the install still succeeds over the decoy" "NEW" "$(persist_marker)"

# NIT N4: the duplicate big() definition here (identical to the one above case
# 12b) was deleted; the earlier definition is the only one.
SB13B="$(mktemp -d)"; mkdir -p "$SB13B/vol2" "$SB13B/a" "$SB13B/b"
mk "$SB13B/vol2/mod-worker" v1.0.84 OLD
big "$SB13B/a/mod-worker" v1.0.90 A; big "$SB13B/b/mod-worker" v1.0.95 B
MOD_WORKER_IMAGE_BIN="$SB13B/a/mod-worker" MOD_WORKER_PERSIST_DIR="$SB13B/vol2" sh "$ENTRY" --probe >/dev/null 2>&1 &
p1=$!
MOD_WORKER_IMAGE_BIN="$SB13B/b/mod-worker" MOD_WORKER_PERSIST_DIR="$SB13B/vol2" sh "$ENTRY" --probe >/dev/null 2>&1 &
p2=$!
wait "$p1"; wait "$p2"
whole=no; cmp -s "$SB13B/vol2/mod-worker" "$SB13B/a/mod-worker" && whole=yes
cmp -s "$SB13B/vol2/mod-worker" "$SB13B/b/mod-worker" && whole=yes
check "two concurrent installs land exactly one whole source (never a mix)" yes "$whole"
res13="$(find "$SB13B/vol2" -maxdepth 1 -name '*mod-worker*' ! -name 'mod-worker' | wc -l | tr -d ' ')"
check "no temp residue after two concurrent installs" "0" "$res13"

# 8. defaults are still the real image/volume paths
grep -q 'MOD_WORKER_IMAGE_BIN:-/usr/local/bin/mod-worker' "$ENTRY"; check "default image path unchanged" 0 $?
grep -q 'MOD_WORKER_PERSIST_DIR:-/var/lib/mod-worker/bin' "$ENTRY"; check "default volume path unchanged" 0 $?

echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
