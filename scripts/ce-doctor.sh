#!/usr/bin/env bash
# =============================================================================
# ce-doctor.sh — 2400RES2d: read-only "why did a CE service die?" report.
#
#   scripts/ce-doctor.sh [--json]
#
# Walks every container in the CE compose project (label
# com.docker.compose.project=<the `name:` in docker-compose.yml>) and prints
# one line per service:
#
#   OK   api 41% of 1.5g
#   WARN api 93% of 1.5g — near its cap
#   FAIL keycloak was OOM-killed (restarts: 2)
#
# plus the exact fix line for every WARN/FAIL, using ce-resize.sh's OWN
# service -> CE_*_MEM map (lifted out of scripts/ce-resize.sh at runtime —
# one map, no fork):
#
#   -> scripts/ce-resize.sh set api=2.5g
#   -> scripts/ce-resize.sh medium
#
# A stack started with `docker compose -p <name>` carries <name> in the label:
#   CE_PROJECT_NAME=<name> scripts/ce-doctor.sh
#
# Exit: 0 all OK, 1 any WARN, 2 any FAIL (also 2 = bad usage, e.g. an unknown option).
#
# READ-ONLY: runs only the read-only query `docker compose port`, never starts/stops/restarts
# anything, never prints .env values or secrets.
# CE_DOCKER_BIN is a test hook (the docker binary or a shim); never set it in
# production. CE_MEMINFO_PATH is ce-resize's existing meminfo test hook.
# =============================================================================
# 2400CR1x: fail loud on a non-bash shell (this script uses ${BASH_SOURCE[0]}
# below; busybox/dind `sh` chokes on it and SCRIPT_DIR used to come out empty,
# pointing OVERLAY_DIR at the wrong dir). POSIX-only guard, first executable
# line.
[ -n "${BASH_VERSION:-}" ] || { echo "ce-doctor.sh: needs bash (try: apk add bash / apt install bash)" >&2; exit 2; }

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OVERLAY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$OVERLAY_DIR/.env"
COMPOSE_FILE="$OVERLAY_DIR/docker-compose.yml"
RESIZE="$OVERLAY_DIR/scripts/ce-resize.sh"
MEMINFO_PATH="${CE_MEMINFO_PATH:-/proc/meminfo}"
DOCKER_BIN="${CE_DOCKER_BIN:-docker}"
WARN_PCT=90
# A single restart (e.g. a service that came up before its dependency during
# boot) is normal on a healthy box; only RESTART_WARN or more is a warning.
RESTART_WARN=2

# 2400CR1y: ce-doctor lifts detect_profile() out of ce-resize.sh, which is now
# cgroup-aware and calls the shared helper — so source the helper here too
# (min(host, cgroup limit); a 4 GB dind reports 4 GB, not the host's 125).
if [ ! -r "$SCRIPT_DIR/lib/ce-host-resources.sh" ]; then
  echo "ce-doctor: scripts/lib/ce-host-resources.sh missing next to this script — sizing checks would guess from host numbers; copy the whole scripts/ tree (including scripts/lib/)." >&2
  exit 2
fi
# shellcheck source=./lib/ce-host-resources.sh
. "$SCRIPT_DIR/lib/ce-host-resources.sh"

note() { echo "ce-doctor: $1" >&2; }

# --- Reuse ce-resize.sh's map + helpers (lift their source, don't fork it) ---
# RESIZE is always THIS script's sibling (resolved from BASH_SOURCE at the top,
# never from CWD or the environment), so the eval below only ever sees text
# from that one file. lift() is loud: a missing function is a broken map, not
# a silently-empty one.
lift() { # lift <fn> <file> -> that function's source text; non-zero if absent
  local out
  out="$(awk -v fn="$1" 'index($0, fn "() {") == 1 {p = 1} p {print} p && /^}/ {exit}' "$2")"
  [ -n "$out" ] || { note "lift: no ${1}() found in $2"; return 1; }
  printf '%s\n' "$out"
}
if [ -r "$RESIZE" ]; then
  _lifted=""
  _missing=0
  for _fn in svc_var var_svc gb_of detect_profile profile_lines rail_partners; do
    _one="$(lift "$_fn" "$RESIZE")" || _missing=1
    _lifted="${_lifted}${_one}
"
  done
  if [ "$_missing" = 1 ]; then
    note "scripts/ce-resize.sh is present but its map/helpers are incomplete — refusing to guess fix lines."
    exit 2
  fi
  eval "$_lifted"
else
  note "scripts/ce-resize.sh not found — fix lines will name the raw compose service"
fi

# compose service name -> ce-resize's friendly service name
compose_svc_friendly() {
  case "$1" in
    api) echo api ;;
    keycloak) echo keycloak ;;
    postgres-app) echo postgres ;;
    keycloak-db) echo kc-postgres ;;
    redis) echo valkey ;;
    versitygw) echo storage ;;
    mailpit) echo mailpit ;;
    openbao) echo openbao ;;
    openbao-init|keycloak-admin-init) echo init ;;
    mod-worker) echo worker ;;
    mod-dashboard) echo dashboard ;;
    workflow-builder) echo workflowbuilder ;;
    buildkitd) echo buildkitd ;;
    # buildkitd-net is deliberately NOT mapped: its mem_limit is a fixed literal
    # (docker-compose.yml mem_limit: 0.03g) and CE_BUILDKIT_MEM sizes the OTHER
    # buildkit container. The doctor prints "no resize variable for buildkitd-net".
    caddy) echo caddy ;;
    registry) echo registry ;;
    mod-docs) echo docs ;;
    mod-migrate) echo migrate ;;
    ce-node-repair) echo node-repair ;;
    db-owner-repair) echo db-owner-repair ;;
    registry-init) echo init ;;
    *) return 1 ;;
  esac
}
next_profile() { case "$1" in small) echo medium ;; medium) echo large ;; *) return 1 ;; esac; }
fmt_g() { # fmt_g <GB thousandths> -> 2g / 1.5g (odd caps print one decimal)
  local th="$1"
  if [ $((th % 1000)) = 0 ]; then echo "$((th / 1000))g"
  elif [ $((th % 500)) = 0 ]; then echo "$((th / 1000)).5g"
  else echo "$((th / 1000)).$(( (th % 1000) / 100 ))g"; fi
}
profile_total() { # profile_total <profile> -> sum of the mem rails, GB thousandths
  local total=0 line key v
  while IFS= read -r line; do
    key="${line%%=*}"
    case "$key" in CE_API_WORKERS|CE_KC_HEAP|CE_PG_SHARED_BUFFERS|CE_SIZE_PROFILE) continue ;; esac
    v="${line#*=}"
    total=$((total + $(gb_of "$v")))
  done < <(profile_lines "$1")
  echo "$total"
}

json=0
for _a in "$@"; do
  case "$_a" in
    --json) json=1 ;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "ce-doctor: unknown option $_a (usage: ce-doctor.sh [--json])" >&2; exit 2 ;;
  esac
done

# The CE compose project name (the compose file's `name:`; compose uses it for
# the com.docker.compose.project label on every container).
# CE_PROJECT_NAME overrides it for a stack that was started with `docker compose -p <name>`
# (the label then carries <name>, not the compose file's `name:`).
CE_PROJECT="${CE_PROJECT_NAME:-}"
[ -n "$CE_PROJECT" ] || CE_PROJECT="$(grep -m1 '^name:' "$COMPOSE_FILE" 2>/dev/null | cut -d: -f2- | tr -d ' ')"
[ -n "$CE_PROJECT" ] || CE_PROJECT=mod-core-ce


# --- Collect per-container facts (read-only docker calls) -------------------
# -a on: an OOM-killed service is usually already stopped by the time anyone
# looks, and the FAIL cases below are exactly those containers.
# 2400CR1zd: `docker ps` has no `.Name` field — the field is `.Names` (`.Name`
# only exists for `docker stats`/`inspect`). And a FAILING ps must never be
# laundered into "no containers": keep the exit status + the first stderr line.
ps_out="$("$DOCKER_BIN" ps -a --format '{{.Names}}' \
  --filter "label=com.docker.compose.project=$CE_PROJECT" 2>&1)"
ps_rc=$?
if [ "$ps_rc" != 0 ]; then
  [ "$json" = 1 ] && printf '[]\n'
  note "docker ps failed: $(printf '%s\n' "$ps_out" | grep -v '^$' | head -n1)"
  note "re-run once docker works; the doctor cannot check caps/OOM without it."
  exit 2
fi
names="$(printf '%s\n' "$ps_out" | grep -v '^$' || true)"
# A CE_PROJECT_NAME that matches zero containers (e.g. set to the directory name
# while install.sh starts the stack as the compose file's `name:`) is ignored
# with a note: fall back to the compose-file name, the same one install.sh uses.
if [ -z "$names" ] && [ -n "${CE_PROJECT_NAME:-}" ]; then
  compose_name="$(grep -m1 '^name:' "$COMPOSE_FILE" 2>/dev/null | cut -d: -f2- | tr -d ' ')"
  [ -n "$compose_name" ] || compose_name=mod-core-ce
  if [ "$compose_name" != "$CE_PROJECT" ]; then
    ps_alt="$("$DOCKER_BIN" ps -a --format '{{.Names}}' \
      --filter "label=com.docker.compose.project=$compose_name" 2>/dev/null | grep -v '^$' || true)"
    if [ -n "$ps_alt" ]; then
      note "CE_PROJECT_NAME=$CE_PROJECT matches no container; using the compose project '$compose_name' instead ($(printf '%s\n' "$ps_alt" | grep -c .) container(s) found under it: $(printf '%s\n' "$ps_alt" | tr '\n' ' ')); if those belong to a different stack, unset CE_PROJECT_NAME)."
      CE_PROJECT="$compose_name"; names="$ps_alt"
    fi
  fi
fi
if [ -z "$names" ]; then
  [ "$json" = 1 ] && printf '[]\n'
  note "no containers carry label com.docker.compose.project=$CE_PROJECT — nothing to check."
  note "if the stack was never started, run: docker compose up -d (then re-run this doctor)."
  exit 0
fi
# --- Encrypted-store checks (GY.C6.s1: gocryptfs mounts INSIDE each store) ---
# The CE compose binds the host CIPHERTEXT dir at /ciphertext-raw and the 0600
# passphrase file at /run/secrets/gocryptfs-pass, and entrypoint-enc.sh mounts
# gocryptfs at /data INSIDE the container (no sidecar service, no decrypted
# host dir). A store serving an UNMOUNTED /data writes plaintext straight into
# the cipher dir, so the doctor proves the chain before it says anything about
# caps: passfile present + regular + 0600, both cipher dirs exist, /data is
# really a mountpoint in each running store, no plaintext leaked into the
# container layer under /data (ENC-PROOF-LINUX), and the host has /dev/fuse.
# READ-ONLY: `docker exec ... mountpoint -q /data` only asks the kernel about a
# mountpoint. Paths and modes are printed, NEVER contents — the passphrase file
# is never read, cat'ed or copied, and `sudo -n` is never prompted for
# interactively (an unreadable path with no usable sudo is a WARN, never a
# FAIL). CE_PASS_FILE is a test hook for the passfile path (like CE_DOCKER_BIN
# and CE_MEMINFO_PATH); never set it in production.
enc_worst=0
enc_line() { # enc_line <PASS|WARN|FAIL|SKIP> <label> <detail> [fix]
  local st="$1" label="$2" detail="$3" fix="${4:-}" fd=1
  # --json keeps stdout one pure JSON document; these lines go to stderr.
  [ "$json" = 1 ] && fd=2
  case "$st" in
    PASS) printf 'PASS %s %s\n' "$label" "$detail" >&"$fd" ;;
    SKIP) printf 'SKIP %s %s\n' "$label" "$detail" >&"$fd" ;;
    WARN) printf 'WARN %s %s\n' "$label" "$detail" >&"$fd"
          [ -n "$fix" ] && printf '  -> %s\n' "$fix" >&"$fd"
          [ "$enc_worst" -lt 1 ] && enc_worst=1 ;;
    FAIL) printf 'FAIL %s %s\n' "$label" "$detail" >&"$fd"
          [ -n "$fix" ] && printf '  -> %s\n' "$fix" >&"$fd"
          enc_worst=2 ;;
  esac
  return 0
}

SUDO_BIN="$(command -v sudo || true)"
stat_probe() { # stat_probe <path> -> "<kind> <mode>" on stdout
  # kind: file|dir|char|other|missing|denied. A path under an unreadable parent
  # dir is `denied` (stat cannot see it). The FIRST stat result is kept; it is
  # retried through `sudo -n` (never prompts) ONLY when that first error says
  # `Permission denied`, and the sudo result is used ONLY when sudo exits 0.
  # So a genuinely missing path stays `missing` even when sudo needs a password
  # (its own error text must not replace stat's "No such file"), and a
  # permission failure sudo cannot resolve stays `denied` (caller WARNs).
  local p="$1" out rc kind mode sout src
  out="$(stat -c '%F %a' -- "$p" 2>&1)"; rc=$?
  if [ "$rc" != 0 ] && [ -n "$SUDO_BIN" ]; then
    case "$out" in
      *'Permission denied'*)
        sout="$(sudo -n -p '' stat -c '%F %a' -- "$p" 2>&1)"; src=$?
        if [ "$src" = 0 ]; then out="$sout"; rc=0; fi ;;
    esac
  fi
  if [ "$rc" = 0 ]; then
    mode="${out##* }"
    case "$out" in
      'regular file '*) kind=file ;;
      'directory '*) kind=dir ;;
      'character special file '*) kind=char ;;
      *) kind=other ;;
    esac
    printf '%s %s\n' "$kind" "$mode"
  else
    case "$out" in
      *'No such file'*|*'does not exist'*) printf 'missing -\n' ;;
      *) printf 'denied -\n' ;;
    esac
  fi
  return 0
}

compose_var() { # compose_var <KEY> <default> -> env, else .env, else default
  # `local v="${!key}"` in ONE statement expands before key is set (bash
  # expands every word first), so declare, then assign.
  local key="$1" def="$2" v
  v="${!key:-}"
  [ -n "$v" ] || v="$(grep "^${key}=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
  printf '%s' "${v:-$def}"
}

# (a) the passphrase file: regular, 0600, never read or printed.
PASS_FILE="${CE_PASS_FILE:-/run/secrets/gocryptfs-pass}"
read -r pf_kind pf_mode <<<"$(stat_probe "$PASS_FILE")"
case "$pf_kind" in
  file)
    if [ "$pf_mode" = 600 ]; then
      enc_line PASS "passfile" "$PASS_FILE regular 0600"
    else
      enc_line FAIL "passfile" "$PASS_FILE mode $pf_mode, want 0600" \
        "chmod 600 $PASS_FILE (a loose passphrase file lets any other uid open a store)"
    fi ;;
  missing) enc_line FAIL "passfile" "$PASS_FILE missing — both stores refuse to start without it" \
    "run scripts/first-run-secrets.sh (it generates the passphrase ONCE at 0600)" ;;
  other) enc_line FAIL "passfile" "$PASS_FILE exists but is not a regular file" \
    "remove it and re-run scripts/first-run-secrets.sh" ;;
  *) enc_line WARN "passfile" "$PASS_FILE unreadable and sudo cannot verify it — cannot verify mode" \
    "run: sudo scripts/ce-doctor.sh (or make its parent dir readable)" ;;
esac

# (b) the two host CIPHER dirs the compose binds at /ciphertext-raw.
VGW_CIPHER_DIR="$(compose_var VERSITYGW_CIPHER_PATH /var/lib/mod/versitygw-ciphertext)/$(compose_var CE_TENANT_SLUG t1)"
VALKEY_CIPHER_DIR="$(compose_var VALKEY_CIPHER_PATH /var/lib/mod/valkey-ciphertext)/data"
cipher_check() { # cipher_check <label> <dir>
  local label="$1" dir="$2" kind mode
  read -r kind mode <<<"$(stat_probe "$dir")"
  case "$kind" in
    dir) enc_line PASS "cipher $label" "$dir exists" ;;
    missing) enc_line FAIL "cipher $label" "$dir missing — the store cannot mount over it" \
      "run scripts/first-run-secrets.sh (it creates the cipher dir at 0700)" ;;
    denied) enc_line WARN "cipher $label" "$dir cannot be verified (unreadable, no usable sudo)" \
      "run: sudo scripts/ce-doctor.sh (or make its parent dir readable)" ;;
    *) enc_line FAIL "cipher $label" "$dir exists but is not a directory" \
      "remove it and re-run scripts/first-run-secrets.sh" ;;
  esac
  return 0
}
cipher_check versitygw "$VGW_CIPHER_DIR"
cipher_check valkey "$VALKEY_CIPHER_DIR"

# (f) the PERSISTENT key file (FIX-SIDECAR-PASSFILE-GUARD, compose side 7689f4f5e):
# /run is tmpfs, so the passphrase the stores read (block (a), /run/secrets/
# gocryptfs-pass) is only a COPY. The source of truth is the disk-backed
# /var/lib/mod/gocryptfs-pass (0600, root-owned, BESIDE - never inside - the
# cipher dirs); first-run-sidecar.sh creates it and rebuilds the /run copy, and
# recover-after-reboot.sh rebuilds the copy after a reboot. The doctor proves the
# persistent file AND that the copy still matches it. cksum ONLY - the file is
# never read, cat'ed or printed, and a checksum is not the passphrase.
#   PASS  regular + 0600 + root-owned, and the /run copy (if present) has the
#         same cksum.
#   WARN  persistent file missing and BOTH cipher dirs are EMPTY: a fresh box
#         that never encrypted anything yet (first-run-secrets.sh will create it).
#         "EMPTY" means PROVEN empty: a cipher dir this uid cannot list is a
#         separate WARN ("cannot read ... cannot tell if it holds data"), never
#         a fresh box - a lost key plus ciphertext is data loss (R15).
#   FAIL  ciphertext exists and the persistent file is missing: the data is
#         unreadable without the ORIGINAL key, so the fix is "restore it from
#         your backup", never "generate a new one".
#   FAIL  mode not 0600 / not a regular file / not root-owned / the /run copy
#         holds a DIFFERENT key (cksum differs). The copy mismatch is a FAIL,
#         not a WARN, on purpose: a store can be serving happily right now with
#         the copy that the NEXT REBOOT destroys, after which the persistent
#         file cannot unlock that data - silent data loss, so it must be loud.
# CE_KEY_FILE / CE_KEY_OWNER_UID are test hooks in the same style as CE_PASS_FILE
# (a path and an expected owner uid); never set them in production.
KEY_FILE="${CE_KEY_FILE:-/var/lib/mod/gocryptfs-pass}"
KEY_OWNER_WANT="${CE_KEY_OWNER_UID:-0}"
key_cksum() { # key_cksum <path> -> "<cksum> <bytes>" on stdout, empty if unreadable
  # cksum prints "<sum> <bytes> <path>"; the PATH is dropped so two files at
  # different paths compare by CONTENT only (and no secret is ever printed - a
  # checksum is not the passphrase).
  local p="$1" out rc
  out="$(cksum -- "$p" 2>/dev/null)"; rc=$?
  if [ "$rc" != 0 ] && [ -n "$SUDO_BIN" ]; then
    out="$(sudo -n -p '' cksum -- "$p" 2>/dev/null)"; rc=$?
  fi
  [ "$rc" = 0 ] && printf '%s' "$(printf '%s' "$out" | cut -d' ' -f1,2)"
  return 0
}
key_owner() { # key_owner <path> -> owner uid on stdout, empty if unknown
  # `stat -c %u` prints a NAME when the uid is in /etc/passwd and a NUMBER
  # otherwise, so "root" is folded back to 0 here: the comparison is always
  # numeric and never depends on this box's passwd file.
  local p="$1" out rc
  out="$(stat -c '%u' -- "$p" 2>/dev/null)"; rc=$?
  if [ "$rc" != 0 ] && [ -n "$SUDO_BIN" ]; then
    out="$(sudo -n -p '' stat -c '%u' -- "$p" 2>/dev/null)"; rc=$?
  fi
  [ "$out" = root ] && out=0
  [ "$rc" = 0 ] && printf '%s' "$out"
  return 0
}
cipher_dir_state() { # cipher_dir_state <dir> -> empty | entries | denied
  # TRI-STATE on purpose (R15, data safety): "no entries" may only be printed
  # for a dir this uid could actually list. A root-0700 cipher dir run as a
  # non-root user lists NOTHING through a plain glob, so a plain-glob helper
  # would report ciphertext with a lost key as a fresh box (a WARN) instead of
  # the FAIL the header rule demands. The sudo retry is the same `sudo -n`
  # style stat_probe uses: it never prompts, and a sudo that needs a password
  # exits non-zero, which stays `denied` (a WARN, never a FAIL).
  local d="$1" kind _ g out rc
  read -r kind _ <<<"$(stat_probe "$d")"
  case "$kind" in
    dir) ;;
    missing) printf 'missing\n'; return 0 ;;
    *) printf 'denied\n'; return 0 ;;
  esac
  # No `ls` on the plain path (the doctor's externals list is deliberately
  # tiny): a non-matching glob stays literal and -e on the literal is false.
  for g in "$d"/* "$d"/.[!.]* "$d"/..?*; do
    [ -e "$g" ] && { printf 'entries\n'; return 0; }
  done
  # Nothing matched: only call it empty when the dir was readable+traversable.
  if [ -r "$d" ] && [ -x "$d" ]; then printf 'empty\n'; return 0; fi
  if [ -n "$SUDO_BIN" ]; then
    out="$(sudo -n -p '' ls -A -- "$d" 2>/dev/null)"; rc=$?
    if [ "$rc" = 0 ]; then
      [ -n "$out" ] && { printf 'entries\n'; return 0; }
      printf 'empty\n'; return 0
    fi
  fi
  printf 'denied\n'
  return 0
}
read -r kf_kind kf_mode <<<"$(stat_probe "$KEY_FILE")"
case "$kf_kind" in
  file)
    kf_owner="$(key_owner "$KEY_FILE")"
    if [ "$kf_mode" != 600 ]; then
      enc_line FAIL "key file" "$KEY_FILE mode $kf_mode, want 0600" \
        "chmod 600 $KEY_FILE (a loose key file lets any other uid on the host open a store)"
    elif [ -n "$kf_owner" ] && [ "$kf_owner" != "$KEY_OWNER_WANT" ]; then
      enc_line FAIL "key file" "$KEY_FILE owned by uid $kf_owner, want uid $KEY_OWNER_WANT (root)" \
        "chown root:root $KEY_FILE (first-run-sidecar.sh creates it root-owned)"
    elif [ ! -s "$KEY_FILE" ]; then
      enc_line FAIL "key file" "$KEY_FILE exists but is EMPTY — no store can unlock its data" \
        "restore $KEY_FILE (mode 0600, root-owned) from your backup; do not generate a new one"
    else
      kf_sum="$(key_cksum "$KEY_FILE")"
      read -r pc_kind _ <<<"$(stat_probe "$PASS_FILE")"
      pc_sum="$(key_cksum "$PASS_FILE")"
      if [ -z "$kf_sum" ]; then
        enc_line WARN "key file" "$KEY_FILE $kf_mode but its checksum cannot be taken (unreadable, no usable sudo) — cannot verify it against the /run copy" \
          "run: sudo scripts/ce-doctor.sh (or make the parent dir of $KEY_FILE readable)"
      elif [ "$pc_kind" = missing ]; then
        enc_line WARN "key file" "the persistent key $KEY_FILE looks good but the /run copy $PASS_FILE is missing (a reboot wipes /run) — the stores will refuse to start until it is rebuilt" \
          "run scripts/first-run-sidecar.sh (it rebuilds $PASS_FILE from $KEY_FILE); recover-after-reboot.sh does this automatically"
      elif [ -z "$pc_sum" ]; then
        enc_line WARN "key file" "$KEY_FILE looks good but the /run copy $PASS_FILE cannot be compared (unreadable, no usable sudo) — cannot verify it matches" \
          "run: sudo scripts/ce-doctor.sh (or make the parent dir of $PASS_FILE readable)"
      elif [ "$kf_sum" != "$pc_sum" ]; then
        enc_line FAIL "key file" "the persistent key $KEY_FILE and the /run copy $PASS_FILE hold DIFFERENT keys (cksum mismatch)" \
          "restore the copy from the persistent file (scripts/first-run-sidecar.sh rebuilds $PASS_FILE from $KEY_FILE); never generate a new passphrase"
      else
        enc_line PASS "key file" "$KEY_FILE regular 0600 owner uid $kf_owner, /run copy $PASS_FILE matches (cksum)"
      fi
    fi ;;
  missing)
    vgw_state="$(cipher_dir_state "$VGW_CIPHER_DIR")"
    valkey_state="$(cipher_dir_state "$VALKEY_CIPHER_DIR")"
    if [ "$vgw_state" = entries ] || [ "$valkey_state" = entries ]; then
      enc_line FAIL "key file" "encrypted data exists but the key file $KEY_FILE is missing: restore it from your backup; do not generate a new one" \
        "restore $KEY_FILE (mode 0600, root-owned) from your backup, then scripts/first-run-sidecar.sh rebuilds the /run copy"
    elif [ "$vgw_state" = denied ] || [ "$valkey_state" = denied ]; then
      # R15: never call an unreadable store "fresh box" - a lost key plus
      # ciphertext is data loss, and "fresh box" would tell the user to just
      # re-run first-run-secrets.sh and overwrite nothing. Say what is unknown.
      unread="$VGW_CIPHER_DIR"
      [ "$vgw_state" != denied ] && unread="$VALKEY_CIPHER_DIR"
      enc_line WARN "key file" "cannot read $unread (re-run with sudo): cannot tell if it holds data — with $KEY_FILE missing, ciphertext there cannot be unlocked" \
        "run: sudo scripts/ce-doctor.sh (a proven-data case is a FAIL: restore $KEY_FILE from your backup, never generate a new one)"
    else
      enc_line WARN "key file" "$KEY_FILE missing and both cipher dirs are empty — nothing encrypted yet (fresh box)" \
        "first-run-secrets.sh creates $KEY_FILE (0600) on the first run; until then the key lives only on /run, which a reboot wipes"
    fi ;;
  other) enc_line FAIL "key file" "$KEY_FILE exists but is not a regular file" \
    "remove it and re-run scripts/first-run-sidecar.sh" ;;
  *) enc_line WARN "key file" "$KEY_FILE unreadable and sudo cannot verify it — cannot verify mode/owner" \
    "run: sudo scripts/ce-doctor.sh (or make its parent dir readable)" ;;
esac

# (c) /data is really a mountpoint inside each RUNNING store. A stopped store is
# SKIP (nothing is serving plaintext), not FAIL.
running_out="$("$DOCKER_BIN" ps --format '{{.Names}}' \
  --filter "label=com.docker.compose.project=$CE_PROJECT" 2>/dev/null)"
running="$(printf '%s\n' "$running_out" | sed 's|^/||' | grep -v '^$' || true)"
enc_store() { # enc_store <container> <label>
  local ctr="$1" label="$2"
  if ! printf '%s\n' "$running" | grep -q "^${ctr}$"; then
    enc_line SKIP "mount $label" "container ${ctr} is not running — nothing is serving"
    return
  fi
  if "$DOCKER_BIN" exec "$ctr" mountpoint -q /data >/dev/null 2>&1; then
    enc_line PASS "mount $label" "/data is a mountpoint inside ${ctr}"
  else
    enc_line FAIL "mount $label" "encrypted mount is not up: store must not be serving" \
      "the gocryptfs mount inside ${ctr} is down. Run: cd ${OVERLAY_DIR} && docker compose restart versitygw (or redis for the Valkey store), then re-run scripts/ce-doctor.sh (entrypoint-enc.sh mounts /data before the store starts); never let it serve an unmounted /data"
  fi
  return 0
}
enc_store my_versitygw versitygw
enc_store my_redis valkey

# (e) ENC-PROOF-LINUX: a store whose cwd is the UNDERLYING dir (the writable
# layer beneath the gocryptfs mount) writes plaintext straight into the
# container layer — Valkey's dump.rdb was seen there. A real gocryptfs mount
# does NOT appear in `docker diff` (it is a separate mount), so any Added/Changed
# path under /data in docker diff IS leaked plaintext in the layer. Only the
# PATHS are printed, never contents.
layer_leak() { # layer_leak <container> <label>
  local ctr="$1" label="$2" hits
  if ! printf '%s\n' "$running" | grep -q "^${ctr}$"; then
    enc_line SKIP "layer $label" "container ${ctr} is not running — nothing wrote into the layer"
    return
  fi
  # `docker diff` prints A (added), C (changed) or D (deleted) per path; an A
  # or C under /data is leaked plaintext. The regex also accepts the +/- form
  # some diff-style outputs use: real docker diff never prints it, so that
  # branch is dead but harmless, and the encrypted-mounts test fixture feeds it.
  hits="$("$DOCKER_BIN" diff "$ctr" 2>/dev/null | grep -E '^([AC+-])[[:space:]]+/data(/|$)' | cut -d' ' -f2- || true)"
  if [ -n "$hits" ]; then
    enc_line FAIL "layer $label" "plaintext written outside the encrypted mount (container layer holds /data files): $(printf '%s' "$hits" | tr '\n' ' ')" \
      "recreate the container: cd ${OVERLAY_DIR} && docker compose rm -sf <service> && docker compose up -d <service>; keep this output for support (the layer holds plaintext the encrypted mount never saw)"
  else
    enc_line PASS "layer $label" "no /data files in the container layer"
  fi
  return 0
}
layer_leak my_versitygw versitygw
layer_leak my_redis valkey

# (d) the host has the FUSE device the compose passes through to both stores.
# CE_FUSE_DEV is the same kind of test hook as CE_PASS_FILE (the device path is
# read-only probed, never written); never set it in production.
FUSE_DEV="${CE_FUSE_DEV:-/dev/fuse}"
read -r fuse_kind fuse_mode <<<"$(stat_probe "$FUSE_DEV")"
case "$fuse_kind" in
  char) enc_line PASS "host /dev/fuse" "present (character device)" ;;
  missing) enc_line FAIL "host /dev/fuse" "no /dev/fuse — gocryptfs cannot mount anywhere" \
    "load the fuse module (modprobe fuse) and install fuse3; both stores need /dev/fuse" ;;
  *) enc_line FAIL "host /dev/fuse" "/dev/fuse exists but is not a character device" \
    "restore the device node (mknod /dev/fuse c 10 229) or let udev recreate it" ;;
esac
stats_out="$(printf '%s\n' "$names" | sed 's|^/||' | xargs -r -n20 "$DOCKER_BIN" stats --no-stream --format '{{.Name}}|{{.MemUsage}}|{{.MemPerc}}' 2>/dev/null | sed 's|^/||' || true)"
inspect_out="$(printf '%s\n' "$names" | sed 's|^/||' | xargs -r -n20 "$DOCKER_BIN" inspect \
  --format '{{.Name}}|{{.State.OOMKilled}}|{{.RestartCount}}|{{.HostConfig.Memory}}|{{.State.ExitCode}}|{{index .Config.Labels "com.docker.compose.service"}}' 2>/dev/null || true)"

bytes_th() { # bytes -> GB thousandths
  local b="${1:-0}"
  [ "$b" = 0 ] && { echo 0; return; }
  echo $(( b * 1000 / 1073741824 ))
}
usage_total_th() { # "1.2GiB / 1.5GiB" -> 1500 (GB thousandths of the cap)
  local tot="${1##*/}" num unit int frac th
  tot="${tot//[[:space:]]/}"
  case "$tot" in
    *GiB) num="${tot%GiB}"; unit=1073741824 ;;
    *MiB) num="${tot%MiB}"; unit=1048576 ;;
    *KiB) num="${tot%KiB}"; unit=1024 ;;
    *B)   num="${tot%B}";   unit=1 ;;
    *) echo 0; return ;;
  esac
  [ -n "$num" ] || { echo 0; return; }
  # decimals (1.5GiB) -> thousandths of the unit, same trick ce-resize's gb_of uses
  int="${num%%.*}"
  if [ "$int" = "$num" ]; then frac=0; else frac="${num#*.}"; fi
  frac="${frac}000"; frac="${frac:0:3}"
  th=$(( ${int:-0} * 1000 + frac ))   # thousandths of <unit>
  echo $(( th * unit / 1073741824 ))  # -> GB thousandths
}

# Current profile (same rule ce-resize uses): .env value, else auto-detect.
profile="$(grep -m1 '^CE_SIZE_PROFILE=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
[ "$(type -t detect_profile)" = function ] && detect_profile
[ -n "$profile" ] || profile="${DETECT_PROFILE:-small}"
ram_gb="${DETECT_RAM_GB:-0}"
[ "$(type -t profile_lines)" = function ] && prof_total="$(profile_total "$profile")" || prof_total=0
bump_ok=0
nxt="$(next_profile "$profile" || true)"
[ -n "$nxt" ] && [ "$ram_gb" -gt 0 ] && [ $((ram_gb * 1000)) -ge $((prof_total * 2)) ] && bump_ok=1

# FIX-CE-DOCTOR-ONESHOT-NONZERO-EXITCODE-FAILS: one-shot setup steps (compose
# `restart: "no"`) run once and exit. A non-zero exit is a failed setup step and
# must FAIL even with restarts=0 (install.sh already notices it). Real one-shots
# by compose line: openbao-init :361, keycloak-admin-init :520, mod-migrate :810,
# db-owner-repair :905, ce-node-repair :1000, registry-init :1433 (profile
# registry). NOT one-shots: video-converter-h264 :1402 and ffmpeg-cpu-h264
# :1408 (entrypoint ["true"] placeholders, no container_name). Keep in sync with
# the compose file; this is a case list, never parsed from compose at runtime.
is_oneshot() { # is_oneshot <container name> -> 0 when it is a one-shot setup step
  case "$1" in
    my_openbao_init|my_keycloak_admin_init|my_ce_migrate|my_ce_db_owner_repair|my_ce_node_repair|mod_registry_init) return 0 ;;
    *) return 1 ;;
  esac
}

status_of() { # 0 OK, 1 WARN, 2 FAIL; sets warn_cap / warn_restart (why it WARNs)
  local oom="$1" restarts="$2" exit="$3" pct="$4"
  warn_cap=0; warn_restart=0
  [ "${pct:-0}" -ge "$WARN_PCT" ] && warn_cap=1
  [ "${restarts:-0}" -ge "$RESTART_WARN" ] && warn_restart=1
  if [ "$oom" = true ] || { [ "${restarts:-0}" -gt 0 ] && [ "$exit" = 137 ]; }; then return 2; fi
  if [ "$warn_cap" = 1 ] || [ "$warn_restart" = 1 ]; then return 1; fi
  return 0
}

# FIX-DOCTOR-LAN-ISSUER-CHECK: the "Invalid issuer" trap. A LAN install that
# sets only KC_HOSTNAME gets tokens whose `iss` is http://<ip>:<port>/... while
# the API only accepts KEYCLOAK_EXTERNAL_URL (default http://localhost:<port>,
# port = KEYCLOAK_HOST_PORT, default 8080).
# Read-only, no docker/network; prints HOSTS only (never scheme/port/path).
host_of() { # host_of <url-or-host> -> bare host (pure parameter expansion)
  local h="$1"
  h="${h#*://}"; h="${h%%/*}"; h="${h%%\?*}"; h="${h%%#*}"; h="${h##*@}"
  case "$h" in
    \[*\]*) h="${h#[}"; h="${h%%]*}" ;;
    *) h="${h%%:*}" ;;
  esac
  printf '%s' "$h"
}
lan_host_check() {
  local kc ext dash api kport
  kport="$(compose_var KEYCLOAK_HOST_PORT 8080)"
  case "$kport" in
    ''|*[!0-9]*)
      enc_line FAIL "lan-hosts" "KEYCLOAK_HOST_PORT is not a number: $kport" "set KEYCLOAK_HOST_PORT to a port number (default 8080), or re-run scripts/install.sh --keycloak-port N"
      return 0 ;;
  esac
  kc="$(host_of "$(compose_var KC_HOSTNAME localhost)")"
  ext="$(host_of "$(compose_var KEYCLOAK_EXTERNAL_URL "http://localhost:$kport")")"
  dash="$(host_of "$(compose_var DASHBOARD_URL http://localhost:3000)")"
  api="$(host_of "$(compose_var MOD_API_URL http://localhost:8000/v1)")"
  local h all_local=1
  for h in "$kc" "$ext" "$dash" "$api"; do
    case "$h" in localhost|127.0.0.1) ;; *) all_local=0 ;; esac
  done
  if [ "$all_local" = 1 ]; then
    enc_line PASS "lan-hosts" "single-machine defaults (all four hosts are localhost)"
    return 0
  fi
  if [ "$kc" = "$ext" ] && [ "$kc" = "$dash" ] && [ "$kc" = "$api" ]; then
    enc_line PASS "lan-hosts" "KC_HOSTNAME, KEYCLOAK_EXTERNAL_URL, DASHBOARD_URL, MOD_API_URL all use host $kc"
    return 0
  fi
  local fix="re-run scripts/install.sh --lan (it sets every address variable from one host)"
  local note_=""
  [ "$kc" != "$ext" ] && note_=" (issuer host mismatch: logins return Invalid issuer)"
  enc_line WARN "lan-hosts" "hosts differ: KC_HOSTNAME=$kc KEYCLOAK_EXTERNAL_URL=$ext DASHBOARD_URL=$dash MOD_API_URL=$api$note_" "$fix"
}

# LAN mode: published binds must match the chosen mode. Infra (postgres-app 5432,
# redis 6379, openbao 8200, mailpit 8025) must ALWAYS be loopback; the browser-facing
# ports must bind CE_BIND_ADDR (127.0.0.1 unless scripts/install.sh --lan chose more).
# D56 (FIX-CE-DOCTOR-BIND-CHECK-PARSE-IPV6-FIRST-PUBLISHED-PORT-LINES...): a
# dual-stack host can print the IPv6 mapping FIRST, e.g. `[::]:6379`, so the old
# `head -n1` + `${got%:*}` idiom read addr=[::] and FALSE-FAILED the infra specs
# (and false-WARNed the browser specs, D55 finding F-1). port_addrs reads EVERY
# published line, addr_ok judges one bare address, and bad_addr (the ONE helper
# every loop below uses) reports the first offending one, so no copy of the old
# idiom is left. Single-line IPv4 output keeps every message byte-identical.
port_addrs() { # port_addrs -> reads compose-port output on stdin, one bare address per line
  local l
  while IFS= read -r l || [ -n "$l" ]; do
    [ -n "$l" ] || continue
    l="${l%->*}"                       # drop a -><port>/<proto> suffix if present
    printf '%s\n' "${l%:*}"           # drop the trailing :<port>
  done
}
addr_ok() { # addr_ok <addr> <expected> -> 0 when the published address is expected
  local a="$1" want="$2"
  [ -z "$a" ] && return 1
  [ "$a" = "$want" ] && return 0
  # any-address mode (CE_BIND_ADDR=0.0.0.0, REGISTRY_BIND default): [::] is the
  # IPv6 equivalent of 0.0.0.0
  [ "$want" = 0.0.0.0 ] && [ "$a" = '[::]' ] && return 0
  # loopback: [::1] is the IPv6 equivalent of 127.0.0.1. A lone [::] is NOT ok
  # here: it exposes the port on every v6 interface (strict-fail, named as-is).
  [ "$want" = 127.0.0.1 ] && [ "$a" = '[::1]' ] && return 0
  return 1
}
bad_addr() { # bad_addr <expected> -> reads compose-port output on stdin, prints first bad address
  local want="$1" lines a out=""
  lines="$(port_addrs)"
  while IFS= read -r a; do
    [ -n "$a" ] || continue
    addr_ok "$a" "$want" || { [ -n "$out" ] || out="$a"; }
  done <<< "$lines"
  printf '%s' "$out"
}
bind_check() {
  command -v "$DOCKER_BIN" >/dev/null 2>&1 || { enc_line SKIP "bind-addrs" "docker not available"; return 0; }
  local want bad="" lan_bad="" opt_bad="" reg_wide="" spec svc cp got addr seen=0
  want="$(compose_var CE_BIND_ADDR 127.0.0.1)"
  for spec in postgres-app:5432 redis:6379 openbao:8200 mailpit:8025; do
    svc="${spec%%:*}"; cp="${spec##*:}"
    got="$(cd "$OVERLAY_DIR" && "$DOCKER_BIN" compose port "$svc" "$cp" 2>/dev/null)"; [ -n "$got" ] && seen=1
    addr="$(bad_addr 127.0.0.1 <<< "$got")"; [ -n "$addr" ] && bad="$bad $svc=$addr"
  done
  for spec in api:80 mod-dashboard:80 workflow-builder:3000 keycloak:8080 versitygw:7070 mod-docs:80; do
    svc="${spec%%:*}"; cp="${spec##*:}"
    got="$(cd "$OVERLAY_DIR" && "$DOCKER_BIN" compose port "$svc" "$cp" 2>/dev/null)"; [ -n "$got" ] && seen=1
    addr="$(bad_addr "$want" <<< "$got")"; [ -n "$addr" ] && lan_bad="$lan_bad $svc=$addr"
  done
  # FIX-COMPOSE-REGISTRY-CADDY-BIND-AND-PORT-DOCS (F-4): the two opt-in profiles were
  # invisible to this check, so with `registry` on the tool printed PASS while 0.0.0.0:5000
  # was live. Both are opt-in, so gate on the service actually RUNNING (compose ps): a
  # default install never sees them and keeps its old PASS wording unchanged. The query
  # itself passes --profile so a profile-gated service resolves when it IS running.
  opt_running="$(cd "$OVERLAY_DIR" && "$DOCKER_BIN" compose --profile stack-ingress --profile registry ps --status running --format '{{.Service}}' 2>/dev/null)"
  # caddy (stack-ingress) is browser-facing: it must follow CE_BIND_ADDR like the six.
  if printf '%s\n' "$opt_running" | grep -qx '^caddy$'; then
    for spec in caddy:8880 caddy:8443; do
      svc="${spec%%:*}"; cp="${spec##*:}"
      got="$(cd "$OVERLAY_DIR" && "$DOCKER_BIN" compose --profile stack-ingress port "$svc" "$cp" 2>/dev/null)"; [ -n "$got" ] && seen=1
      addr="$(bad_addr "$want" <<< "$got")"; [ -n "$addr" ] && opt_bad="$opt_bad $svc=$addr"
    done
  fi
  # registry is wide ON PURPOSE (docs/node-builder.md: other worker machines pull built
  # nodes from it), so it must equal REGISTRY_BIND and a wide bind is a WARN with the
  # reason, never a FAIL.
  if printf '%s\n' "$opt_running" | grep -qx '^registry$'; then
    reg_want="$(compose_var REGISTRY_BIND 0.0.0.0)"
    got="$(cd "$OVERLAY_DIR" && "$DOCKER_BIN" compose --profile registry port registry 5000 2>/dev/null)"
    if [ -n "$got" ]; then
      seen=1
      addr="$(bad_addr "$reg_want" <<< "$got")"; [ -n "$addr" ] && opt_bad="$opt_bad registry=$addr"
      addr="$(bad_addr 127.0.0.1 <<< "$got")"; [ -n "$addr" ] && reg_wide="$reg_wide registry=$addr"
    fi
  fi
  [ "$seen" = 1 ] || { enc_line SKIP "bind-addrs" "no CE container is running (nothing published to check)" "start it with scripts/install.sh"; return 0; }
  if [ -n "$bad" ]; then
    enc_line FAIL "bind-addrs" "databases and the vault are published beyond loopback:$bad" "run: scripts/install.sh --no-lan (databases and vault stay on 127.0.0.1); if you edited a compose override that publishes them, change its port line back to 127.0.0.1:<port>:<port>. Do not delete the file."
  elif [ -n "$lan_bad" ]; then
    enc_line WARN "bind-addrs" "published address differs from CE_BIND_ADDR=$want:$lan_bad" "re-run scripts/install.sh (--lan or --no-lan) to recreate the containers"
  elif [ -n "$opt_bad" ]; then
    enc_line WARN "bind-addrs" "opt-in profile bind differs from its setting:$opt_bad" "the stack-ingress edge follows CE_BIND_ADDR (STACK_INGRESS_BIND to override); registry follows REGISTRY_BIND"
  elif [ -n "$reg_wide" ]; then
    enc_line WARN "bind-addrs" "registry is published beyond loopback:$reg_wide (on purpose: other worker machines pull built nodes from it; set REGISTRY_BIND=127.0.0.1 to keep it on this machine)" "see docs/node-builder.md"
  elif [ "$want" = 127.0.0.1 ] && [ "$(compose_var PUBLIC_HOST localhost)" != localhost ]; then
    enc_line WARN "bind-addrs" "PUBLIC_HOST is $(compose_var PUBLIC_HOST localhost) but ports are localhost-only: other computers cannot connect" "scripts/install.sh --lan"
  else
    enc_line PASS "bind-addrs" "browser ports on $want, databases and the vault on 127.0.0.1"
  fi
}

# BUILDER-DEFAULT-REV-FIXES F3: the node builder is NOT part of the
# encrypted-store verdict. builder_line prints the same PASS/WARN/SKIP shapes as
# enc_line but never touches enc_worst, so a down or missing builder neither
# turns the exit code to 1 nor suppresses "encrypted-store chain looks healthy".
builder_line() { # builder_line <PASS|WARN|SKIP|INFO> <detail> [fix]
  local fd=1; [ "$json" = 1 ] && fd=2
  printf '%s builder %s\n' "$1" "$2" >&"$fd"
  [ -n "${3:-}" ] && printf '  -> %s\n' "$3" >&"$fd"
  return 0
}
builder_check() { # FIX-INSTALL-ENABLE-NODE-BUILDER-DEFAULT: the node builder
  # (buildkitd-net + buildkitd) is ON by default - install.sh writes
  # COMPOSE_PROFILES=builder unless the AppArmor profile could not be installed
  # (then it leaves it out and prints the enable-later command). Disabling the
  # builder on purpose (COMPOSE_PROFILES set WITHOUT builder) is an INFO, never
  # a WARN/FAIL; none of these lines is scored.
  local prof running missing="" svc det fix enable_cmd
  enable_cmd="cd \"$OVERLAY_DIR\" && sudo bash $OVERLAY_DIR/scripts/install-buildkit-apparmor.sh && $OVERLAY_DIR/scripts/install.sh --enable-builder && docker compose --profile builder up -d buildkitd-net buildkitd"
  prof="$(compose_var COMPOSE_PROFILES '')"
  command -v "$DOCKER_BIN" >/dev/null 2>&1 || { builder_line SKIP "docker not available"; return 0; }
  running="$(cd "$OVERLAY_DIR" && "$DOCKER_BIN" compose ps --status running --format '{{.Service}}' 2>/dev/null)"
  for svc in buildkitd-net buildkitd; do
    printf '%s\n' "$running" | grep -q "^${svc}$" || missing="$missing $svc"
  done
  case ",${prof}," in
    *",builder,"*) : ;;
    *) if [ -z "$missing" ]; then
         builder_line INFO "node builder up (buildkitd-net, buildkitd; COMPOSE_PROFILES=${prof:-empty})"
       elif [ -n "$prof" ]; then
         builder_line INFO "node builder not enabled (COMPOSE_PROFILES=$prof): the wizard's node builder stays down; enable it by adding builder to COMPOSE_PROFILES"
       else
         builder_line WARN "node builder not installed (COMPOSE_PROFILES is empty: the builder was left out, usually no sudo for its AppArmor profile)" "$enable_cmd"
       fi
       return 0 ;;
  esac
  if [ -z "$missing" ]; then
    builder_line PASS "node builder up (buildkitd-net, buildkitd)"
    return 0
  fi
  fix="cd \"$OVERLAY_DIR\" && docker compose --profile builder up -d buildkitd-net buildkitd"
  det="$(cd "$OVERLAY_DIR" && "$DOCKER_BIN" compose --profile builder logs --tail 20 buildkitd 2>/dev/null | grep -i -m1 'apparmor\|permission denied' || true)"
  [ -n "$det" ] && fix="$fix; if the host gates user namespaces: sudo bash $OVERLAY_DIR/scripts/install-buildkit-apparmor.sh"
  builder_line WARN "node builder down:$missing" "$fix"
}

# FIX-CE-DOCTOR-FREE-DISK-CHECK: free space on the filesystem holding docker's
# data root. install.sh checks 20 GiB once, before the pull (the install gate);
# this WARNs below 8 GiB and FAILs below 3 GiB, so a box that just passed the
# install gate does not WARN on day 1 (first pull is ~9 GB), while container
# logs, the OpenBao audit log and old image layers keep growing. Same probe as install.sh (docker info DockerRootDir, df -Pk, awk
# NR==2 {print $4}); if df does not answer nothing is printed (skipped silently).
# The thresholds are plain shell variables, NOT environment knobs. disk_worst is
# separate from enc_worst so a low disk never suppresses the encrypted-store note.
disk_worst=0
disk_check() {
  local fd=1 droot free_kb gib warn_kb=8388608 fail_kb=3145728 fix
  [ "$json" = 1 ] && fd=2
  droot="$("$DOCKER_BIN" info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
  [ -n "$droot" ] || droot="$OVERLAY_DIR"
  free_kb="$(df -Pk "$droot" 2>/dev/null | awk 'NR==2{print $4}')"
  case "$free_kb" in (''|*[!0-9]*) return 0 ;; esac # probe unavailable: skipped silently
  gib=$((free_kb / 1048576))
  fix="free space without touching audit logs or volumes: docker system df; docker image prune (dangling images only); see Container log rotation in the docs. Never delete the OpenBao audit log or any volume."
  if [ "$free_kb" -lt "$fail_kb" ]; then
    printf 'FAIL disk: only %s GiB free on %s (under 3 GiB)\n  -> %s\n' "$gib" "$droot" "$fix" >&"$fd"
    disk_worst=2
  elif [ "$free_kb" -lt "$warn_kb" ]; then
    printf 'WARN disk: only %s GiB free on %s (under 8 GiB)\n  -> %s\n' "$gib" "$droot" "$fix" >&"$fd"
    disk_worst=1
  else
    printf 'PASS disk: %s GiB free on %s\n' "$gib" "$droot" >&"$fd"
  fi
  return 0
}

worst=0
first=1
[ "$json" = 1 ] && printf '['
while IFS='|' read -r name oom restarts cap_bytes exit_code svc; do
  [ -n "$name" ] || continue
  name="${name#/}"
  usage="$(printf '%s\n' "$stats_out" | grep -m1 "^${name}|" || true)"
  memusage="$(cut -d'|' -f2 <<<"$usage" || true)"
  memperc="$(cut -d'|' -f3 <<<"$usage" || true)"
  pct="${memperc%\%}"; pct="${pct//[[:space:]]/}"; pct="${pct%%.*}"; [ -n "$pct" ] || pct=0
  cap_th="$(bytes_th "${cap_bytes:-0}")"
  [ "$cap_th" = 0 ] && cap_th="$(usage_total_th "${memusage#* / }")"
  cap_disp=$([ "$cap_th" = 0 ] && echo "no cap" || echo "$(fmt_g "$cap_th")")
  status_of "$oom" "${restarts:-0}" "${exit_code:-0}" "$pct"; st=$?
  # one-shot exit 0 = finished setup (OK, as status_of gives); non-zero = FAIL
  oneshot_fail=0
  if is_oneshot "$name" && [ "${exit_code:-0}" != 0 ] && [ "$oom" != true ]; then
    oneshot_fail=1; st=2
  fi
  [ "$st" -gt "$worst" ] && worst="$st"
  friendly="$(compose_svc_friendly "$svc" || true)"
  key=""; [ -n "$friendly" ] && key="$(svc_var "$friendly" || true)"
  [ -n "$key" ] || key="${svc}"
  fix=""
  # a failed one-shot is not a memory problem: no resize advice for it; a WARN
  # for restarts alone (memory well below the cap) is not one either
  if [ "$st" -gt 0 ] && [ "$cap_th" -gt 0 ] && [ "$oneshot_fail" = 0 ] && \
     { [ "$st" = 2 ] || [ "$warn_cap" = 1 ]; }; then
    if [ -z "$friendly" ] || [ -z "$(svc_var "$friendly" 2>/dev/null || true)" ]; then
      # no CE_* resize variable behind this service's mem_limit: never print a set command
      fix="no resize variable for ${svc}"
    else
      want=$(( (cap_th * 3 / 2 + 499) / 500 * 500 ))
      fix="scripts/ce-resize.sh set ${friendly}=$(fmt_g "$want")  (${key})"
      partners="$(rail_partners "$svc" || true)"
      [ -n "$partners" ] && fix="$fix — shared rail: $key also resizes $partners"
      [ "$bump_ok" = 1 ] && fix="$fix | scripts/ce-resize.sh $nxt"
    fi
  fi
  # OOM-killed with no memory cap behind it: still say what to do next
  if [ -z "$fix" ] && [ "$st" = 2 ] && [ "$oneshot_fail" = 0 ]; then
    fix="no ce-resize knob for this service: free host memory or stop other services; scripts/ce-resize.sh show lists the knobs."
  fi
  if [ "$json" = 1 ]; then
    [ "$first" = 0 ] && printf ','
    first=0
    printf '{"service":"%s","status":"%s","mem_perc":%s,"cap":"%s","oom_killed":%s,"restarts":%s,"exit_code":%s,"fix":"%s"}' \
      "$svc" "$([ "$st" = 0 ] && echo OK || { [ "$st" = 1 ] && echo WARN || echo FAIL; })" "$pct" "$cap_disp" "${oom:-false}" "${restarts:-0}" "${exit_code:-0}" "$fix"
  else
    why=""
    [ "$warn_cap" = 1 ] && why="near its cap"
    [ "$warn_restart" = 1 ] && why="${why:+$why }restarted=${restarts} (see: docker compose logs ${svc})"
    case "$st" in
      0) printf 'OK   %s %s%% of %s\n' "$svc" "$pct" "$cap_disp" ;;
      1) printf 'WARN %s %s%% of %s — %s\n' "$svc" "$pct" "$cap_disp" "$why" ;;
      2) if [ "$oneshot_fail" = 1 ]; then
           printf 'FAIL %s exited %s (a one-shot setup step failed: docker compose logs %s)\n' "$svc" "$exit_code" "$svc"
         else
           printf 'FAIL %s was OOM-killed (restarts: %s)\n' "$svc" "${restarts:-0}"
         fi ;;
    esac
    if [ -n "$fix" ]; then
      printf '  -> %s\n' "${fix%% | *}"
      [ "$bump_ok" = 1 ] && printf '  -> %s\n' "${fix##* | }"
    fi
  fi
done <<< "$inspect_out"

if [ "$json" = 1 ]; then printf ']\n'; fi
[ "$worst" = 0 ] && note "all CE services are within their memory caps."
lan_host_check
bind_check
builder_check
disk_check
[ "$enc_worst" = 0 ] && note "encrypted-store chain looks healthy (passfile 0600, cipher dirs, /data mounts, /dev/fuse)."
# The encrypted-mount checks are part of the same verdict: a store serving an
# unmounted /data is a FAIL even when every memory cap is fine.
[ "$enc_worst" -gt "$worst" ] && worst="$enc_worst"
[ "$disk_worst" -gt "$worst" ] && worst="$disk_worst"
exit "$worst"
