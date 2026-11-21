#!/usr/bin/env bash
# =============================================================================
# first-run-sidecar.sh — CE host-side first-run support for the ENCRYPTED STORES
# (ENC-FIRSTRUN; the in-container design from commit 2047a28c9, owner 11-10 "redesign
# it"). Called by scripts/first-run-secrets.sh on every run; it can also be run on
# its own from open-core/overlay/.
#
# The CE compose (open-core/overlay/docker-compose.yml, services `versitygw` and
# `redis`) binds exactly THREE host paths that nothing else in CE creates:
#   ${VERSITYGW_CIPHER_PATH:-/var/lib/mod/versitygw-ciphertext}/<slug>  -> /ciphertext-raw (rw)
#   ${VALKEY_CIPHER_PATH:-/var/lib/mod/valkey-ciphertext}/data          -> /ciphertext-raw (rw)
#   /var/lib/mod/gocryptfs-pass (HOST file)     -> /run/secrets/gocryptfs-pass :ro (container target)
# Valkey has no tenant slug: the compose binds a fixed `data` leaf under its base,
# and it shares the ONE passphrase file with versitygw.
#
# There is NO decrypted host dir any more. Each store mounts gocryptfs at /data
# INSIDE its own container (CORE/DockerContainers/encrypted-store/entrypoint-enc.sh),
# so nothing is shared between containers and no `:rshared`/`:rslave` propagation is
# needed - which is exactly what Docker Desktop / WSL2 cannot do. This script creates
# ONLY what that compose binds: the two CIPHERTEXT dirs and the 0600 passphrase file.
#
# Cipher dirs are created 0700, root-owned: the entrypoint mounts as root BEFORE it
# drops privileges (SIDECAR-J5 proved a root 0700 cipher dir works), and a 0700 dir
# keeps another user's tenant ciphertext unreadable.
#
# Idempotent, never destructive: an existing dir is never chmod-ed and an existing
# passfile is never read, rewritten, re-generated or printed. It does NOT run
# `gocryptfs -init` - the store entrypoint inits exactly once over a virgin cipher
# dir, and a second -init over existing ciphertext would destroy the store.
#
# CE has no Vault Agent (OpenBao uses AppRole auth), so the passphrase is a host-owned
# 0600 file. It is NEVER printed, never put in .env, never logged.
#
# KEY PERSISTENCE (FIX-SIDECAR-PASSFILE-GUARD): /run is tmpfs, so a passfile that
# lived ONLY at /run/secrets/gocryptfs-pass vanished on every host reboot while the
# ciphertext survived, and a re-run minted a NEW key = permanent data loss. The
# source of truth is now a root-owned 0600 file in the SAME persistent state dir as
# the cipher dirs: PINNED to /var/lib/mod/gocryptfs-pass regardless of
# VERSITYGW_CIPHER_PATH (the compose bind source is that same fixed host file, so a
# custom cipher base can never move the key); /run/secrets/gocryptfs-pass is only the
# container-side TARGET of that bind (the compose host source is the persistent file),
# kept as a copy restored on every run (and by recover-after-reboot.sh).
# A NEW key is generated (or an existing one REPLACED/adopted) only when BOTH cipher
# dirs are PROVEN virgin - absent, or a dir that lists no entries. A dir that holds
# entries has data; a dir this user cannot LIST is UNKNOWN and is treated exactly like
# data: the script refuses, exits 1 and touches no key file (re-run as root/sudo
# so the dir can be listed). If ciphertext exists but no key file does, the script
# stops and tells the operator to restore the key file from backup.
# The caller, first-run-secrets.sh:135, turns that exit 1 into a WARN and carries on
# (WARN-only: a sidecar failure never aborts first-run-secrets.sh).
#
# Test hooks (FIX-SIDECAR-CIPHER-PATH-AND-UNSAFE; never set in production, and
# production CANNOT set them): SIDECAR_PASS_FILE / SIDECAR_PERSIST_PASS_FILE redirect
# the two key paths into a scratch dir, but ONLY when SIDECAR_TEST_MODE=1 is set AND
# the euid is NOT 0 (a test runs as an unprivileged user). A plain run, and even a
# `sudo -E` run (euid 0 or no test flag), IGNORES both hooks and uses the pinned
# paths, so no production path can move the key files. The two path bases
# VERSITYGW_CIPHER_PATH / VALKEY_CIPHER_PATH redirect the host dirs to scratch dirs.
# =============================================================================
[ -n "${BASH_VERSION:-}" ] || { echo "first-run-sidecar.sh: needs bash (try: apk add bash / apt install bash)" >&2; exit 2; }

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OVERLAY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
ENV_FILE="${ENV_FILE:-$OVERLAY_DIR/.env}"

die() {
  echo "first-run-sidecar: ERROR: $1" >&2
  exit 1
}

# sudo's default env_reset drops shell-exported paths/slugs, so the printed sudo
# line must carry the assignments itself (row 22): every path/slug var set in the
# environment is re-stated after `sudo`, values quoted, never the passphrase.
sudo_env_prefix() { # sudo_env_prefix -> ' KEY=%q' for each env-set resolve var ("" if none)
  local k out=""
  for k in VERSITYGW_CIPHER_PATH VALKEY_CIPHER_PATH CE_TENANT_SLUG; do
    [ -n "${!k:-}" ] && out="$out $(printf '%s=%q' "$k" "${!k}")"
  done
  printf '%s' "$out"
}

env_get() { # env_get <KEY> -> last .env value for KEY ("" if absent); never printed by callers
  [ -f "$ENV_FILE" ] || return 0
  grep -E "^${1}=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- || true
}

# Environment wins, then .env (compose resolves the same two ways), then the
# compose default.
resolve() { # resolve <KEY> <default> -> value
  local key="$1" def="$2" v
  v="${!key:-}"
  [ -n "$v" ] || v="$(env_get "$key")"
  printf '%s' "${v:-$def}"
}

CIPHER_BASE="$(resolve VERSITYGW_CIPHER_PATH /var/lib/mod/versitygw-ciphertext)"
VALKEY_CIPHER_BASE="$(resolve VALKEY_CIPHER_PATH /var/lib/mod/valkey-ciphertext)"
SLUG="$(resolve CE_TENANT_SLUG t1)"
PASS_FILE="/run/secrets/gocryptfs-pass"
# Persistent source of truth: PINNED to the compose bind source /var/lib/mod/gocryptfs-pass
# - NEVER derived from VERSITYGW_CIPHER_PATH (compose hard-codes that host path, so a
# custom cipher base used to put the key where nothing reads it: FIX-SIDECAR-CIPHER-PATH).
PERSIST_FILE="/var/lib/mod/gocryptfs-pass"
# Test hooks (FIX-SIDECAR-CIPHER-PATH-AND-UNSAFE): SIDECAR_PASS_FILE / SIDECAR_PERSIST_PASS_FILE
# redirect the two key paths into a scratch dir, but ONLY under SIDECAR_TEST_MODE=1 AND a
# non-root euid. Production code paths can never move the key files: a plain run or even
# `sudo -E` (euid 0, or no test flag) ignores both hooks and uses the pinned paths.
if [ "${SIDECAR_TEST_MODE:-}" = 1 ] && [ "$(id -u)" -ne 0 ]; then
  PASS_FILE="${SIDECAR_PASS_FILE:-$PASS_FILE}"
  PERSIST_FILE="${SIDECAR_PERSIST_PASS_FILE:-$PERSIST_FILE}"
fi
# FIX-CE-REBOOT-RECOVERY-ENCSTORE-UNIT-BEFORE-DOCKER-AND-DIR-GUARD: a reboot leaves an
# EMPTY DIRECTORY at the /run copy (dockerd auto-creates the bind SOURCE on tmpfs before
# it mounts, and `-s` is TRUE for a 40-byte directory), so the old code read the wiped
# key as "present" and never restored it. A directory at the key path is a boot artifact:
# rmdir it (never a recursive delete) so the -s tests below see it as missing and the
# restore path rebuilds the 0600 FILE from the persistent key file.
if [ -d "$PASS_FILE" ]; then
  echo "sidecar: $PASS_FILE is a DIRECTORY (a boot artifact of the bind mount, not a key) -> removing it so the copy is restored from $PERSIST_FILE"
  rmdir "$PASS_FILE" 2>/dev/null || die "could not remove the directory at $PASS_FILE (it is not empty or is in use). Remove it yourself (rmdir $PASS_FILE) and run scripts/first-run-sidecar.sh again. Nothing was created or changed."
fi

# A slug becomes a PATH component, so it must be a plain token: no '/', no
# ';', no spaces, no leading '-'. t1 (the CE default) and t<tenant_id> pass.
if [[ ! "$SLUG" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]]; then
  die "CE_TENANT_SLUG='$SLUG' is not a safe directory name (letters/digits/_/- only, not starting with -). Set CE_TENANT_SLUG in overlay/.env to a valid slug, then run scripts/first-run-sidecar.sh again."
fi
for base in "$CIPHER_BASE" "$VALKEY_CIPHER_BASE"; do
  [ -n "$base" ] && [ "${base#/}" != "$base" ] && [ "$base" != "/" ] || die "path base '$base' is not an absolute path other than /. Set the base in overlay/.env to an absolute path, then run scripts/first-run-sidecar.sh again."
done

CIPHER_DIR="$CIPHER_BASE/$SLUG"
# Valkey has no tenant slug: the compose binds a fixed `data` leaf under its base.
VALKEY_CIPHER_DIR="$VALKEY_CIPHER_BASE/data"

# --- privilege: root owns these paths; a non-root run says the exact fix -----
writable_p() { # writable_p <path> -> 0 if we can create it without privilege
  local p="$1" d
  if [ -e "$p" ]; then
    if [ -w "$p" ]; then return 0; fi
    return 1
  fi
  d="$(dirname "$p")"
  while [ ! -d "$d" ]; do d="$(dirname "$d")"; done
  if [ -w "$d" ]; then return 0; fi
  return 1
}

if [ "$(id -u)" -ne 0 ]; then
  for p in "$CIPHER_DIR" "$VALKEY_CIPHER_DIR" "$PASS_FILE" "$PERSIST_FILE"; do
    [ -e "$p" ] && continue
    writable_p "$p" && continue
    echo "first-run-sidecar: needs root: $p"
    echo "first-run-sidecar: these host paths need root. Run exactly this one line (the passphrase is generated by it, never by you):"
    echo ""
    printf '  sudo%s bash %q\n' "$(sudo_env_prefix)" "$SCRIPT_PATH"
    echo ""
    echo "  Keep these values in the .env file in the install folder, or run 'sudo -E bash <script>' to preserve env."
    echo ""
    exit 1
  done
fi

# --- cannot-read: the plain, non-scary refusal (nothing is written) -----------
# The key files are 0600 root-owned and the cipher dirs 0700 root-owned, so the
# normal-user re-run the README documents (run the sudo line, then run this script
# again as yourself) cannot read or list them. That is NOT evidence that two keys
# differ or that data exists: it is UNKNOWN. Say so in plain words, exit with the
# existing refusal code (1), and create/change nothing.
need_sudo_die() { # need_sudo_die <reason> -> plain message + exit 1, nothing written
  echo "first-run-sidecar: $1"
  echo "first-run-sidecar: these host paths are root-owned (cipher dirs 0700, key files 0600), so this user cannot read them."
  echo "Run exactly this one line, then run this script again:"
  echo ""
  printf '  sudo%s bash %q\n' "$(sudo_env_prefix)" "$SCRIPT_PATH"
  echo ""
  die "cannot read them as this user: re-run with sudo. Nothing was created or changed."
}

# --- GUARD: never mint a key over existing ciphertext ------------------------
# Decide BEFORE anything is created. A cipher dir has THREE states: an empty dir is
# virgin (safe to mint), a dir that lists entries holds data (never mint), and a dir
# this user cannot list is UNKNOWN - unknown is NOT "has data" (the old code said
# "has data", which turned a normal-user re-run into a false "DIFFERENT passphrases"
# error) but it is not safe either: the guard fails closed and asks for sudo.
# FOUR states, and only two of them allow a key to be created or replaced:
# absent (nothing there yet) and empty (a dir that lists no entries) are PROVEN
# virgin. data holds ciphertext. unreadable is UNKNOWN - it is NOT "no data":
# this user could not list the dir, so it may well hold ciphertext. Every branch
# that CREATES or REPLACES a key file must prove absent|empty for BOTH dirs;
# data and unreadable both fail closed, before any write.
cipher_state() { # cipher_state <dir> -> absent | empty | data | unreadable
  local d="$1" listing
  [ -e "$d" ] || { printf 'absent'; return 0; }
  [ -d "$d" ] || { printf 'empty'; return 0; } # a file here is reported by create_dir below
  if listing="$(ls -A "$d" 2>/dev/null)"; then
    if [ -n "$listing" ]; then printf 'data'; else printf 'empty'; fi
    return 0
  fi
  printf 'unreadable'
}
cipher_has_data() { # cipher_has_data <dir> -> 0 only when it really lists entries
  [ "$(cipher_state "$1")" = data ]
}
cipher_unknown() { # cipher_unknown <dir> -> 0 when this user cannot list it
  [ "$(cipher_state "$1")" = unreadable ]
}
cipher_virgin() { # cipher_virgin <dir> -> 0 ONLY when the dir is proven absent or empty
  local s; s="$(cipher_state "$1")"
  [ "$s" = absent ] || [ "$s" = empty ]
}
cipher_dirs_virgin() { # both stores' cipher dirs proven absent/empty (nothing depends on a key)
  cipher_virgin "$CIPHER_DIR" && cipher_virgin "$VALKEY_CIPHER_DIR"
}
refuse_new_key() { # refuse_new_key <what-would-be-written> -> fail closed unless BOTH dirs are proven virgin
  local act="$1"
  if cipher_unknown "$CIPHER_DIR" || cipher_unknown "$VALKEY_CIPHER_DIR"; then
    need_sudo_die "cannot list a cipher dir ($CIPHER_DIR, $VALKEY_CIPHER_DIR): cannot tell whether encrypted data is already there, so $act is refused. The key files are NOT touched. Re-run as root (sudo) so the dirs can be listed, then run this script again."
  fi
  if ! cipher_virgin "$CIPHER_DIR" || ! cipher_virgin "$VALKEY_CIPHER_DIR"; then
    die "refusing to $act: encrypted data already exists ($CIPHER_DIR or $VALKEY_CIPHER_DIR). Generating or replacing a passphrase would make that data unreadable forever. Restore the passphrase file the data was created with (mode 0600, root-owned) and run this script again. Nothing was created or changed."
  fi
}
if [ -e "$PERSIST_FILE" ] && [ ! -f "$PERSIST_FILE" ]; then die "$PERSIST_FILE exists but is not a regular file. Remove it or restore the passphrase file (mode 0600, root-owned), then run scripts/first-run-sidecar.sh again. Nothing was created or changed."; fi
if [ ! -s "$PERSIST_FILE" ] && [ ! -s "$PASS_FILE" ]; then
  if cipher_unknown "$CIPHER_DIR" || cipher_unknown "$VALKEY_CIPHER_DIR"; then
    need_sudo_die "a cipher dir exists but this user cannot list it ($CIPHER_DIR, $VALKEY_CIPHER_DIR): cannot tell whether encrypted data is already there, so no new key can be generated."
  fi
  if cipher_has_data "$CIPHER_DIR" || cipher_has_data "$VALKEY_CIPHER_DIR"; then
    die "the encrypted data already exists ($CIPHER_DIR or $VALKEY_CIPHER_DIR) but its key file is missing: expected $PERSIST_FILE (or the copy $PASS_FILE). Restore that passphrase file from your backup (mode 0600, root-owned) and run this script again. Do NOT generate a new one: a new passphrase would make the existing encrypted data unreadable forever. Nothing was created or changed."
  fi
fi

# --- two key files present: compare, but "cannot read" is NOT "different" -----
# cmp -s returns 2 when it cannot OPEN a file; the old code read that as "different"
# and died with a false, scary error on the README's second run. Try a non-interactive
# sudo compare first (a stranger's box usually allows it), and when even that is
# unavailable say plainly that the key file cannot be read as this user.
cmp_keys() { # cmp_keys <a> <b> -> 0 identical, 1 different, 2 cannot read
  local a="$1" b="$2" r
  if [ -r "$a" ] && [ -r "$b" ]; then
    r=0; cmp -s "$a" "$b" || r=$?
    if [ "$r" = 0 ]; then return 0; fi
    if [ "$r" = 1 ]; then return 1; fi
    return 2
  fi
  if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    r=0; sudo -n cmp -s "$a" "$b" || r=$?
    if [ "$r" = 0 ]; then return 0; fi
    if [ "$r" = 1 ]; then return 1; fi
  fi
  return 2
}
adopt_run_key=0
if [ -s "$PERSIST_FILE" ] && [ -s "$PASS_FILE" ]; then
  cmp_rc=0; cmp_keys "$PERSIST_FILE" "$PASS_FILE" || cmp_rc=$?
  if [ "$cmp_rc" = 2 ]; then
    need_sudo_die "both key files exist but this user cannot read them ($PERSIST_FILE, $PASS_FILE): cannot tell whether they hold the same passphrase."
  fi
  if [ "$cmp_rc" = 1 ]; then
    # Two different keys. ADOPTING one replaces a key file, so it is allowed only when
    # BOTH cipher dirs are PROVEN virgin (absent or empty): with nothing encrypted yet,
    # nothing depends on either key, so the /run copy is adopted as the persistent key
    # (compose binds the persistent file directly; the /run copy is never read by a
    # container). "unreadable" is NOT "no data": this user could not list the dir, so it
    # may hold ciphertext - fail closed and touch nothing. Only a proven-data case gets
    # the DIFFERENT-passphrases refusal.
    if ! cipher_dirs_virgin; then
      if cipher_unknown "$CIPHER_DIR" || cipher_unknown "$VALKEY_CIPHER_DIR"; then
        need_sudo_die "the two key files differ but a cipher dir ($CIPHER_DIR, $VALKEY_CIPHER_DIR) cannot be LISTED by this user: it is unknown whether encrypted data is already there, so NEITHER key may be replaced. The key files are NOT touched. Re-run as root (sudo) so the dirs can be listed, then run this script again."
      fi
      die "$PERSIST_FILE and $PASS_FILE both exist but hold DIFFERENT passphrases. Refusing to pick one: only the passphrase the data was created with can unlock it. Restore the right file from your backup (remove the wrong one) and run this script again. Nothing was changed."
    fi
    adopt_run_key=1
  fi
fi

# --- create the two cipher dirs, idempotently, with deliberate modes --------
made_dirs=""
kept_dirs=""
create_dir() { # create_dir <path> <mode> — never chmods an existing dir
  local p="$1" mode="$2"
  if [ -d "$p" ]; then kept_dirs="$kept_dirs $p"; return 0; fi
  [ -e "$p" ] && die "path $p exists but is not a directory (a file where a bind mount is expected). Remove or restore that path as a directory, then run scripts/first-run-sidecar.sh again."
  mkdir -p "$p"
  chmod "$mode" "$p"
  made_dirs="$made_dirs $p"
}
create_dir "$CIPHER_DIR" 0700
create_dir "$VALKEY_CIPHER_DIR" 0700

# --- the passphrase: persistent file is the truth, /run copy follows it ------
write_secret() { # write_secret <src|-> <dest>: 0600 AT CREATION; the value never touches stdout
  local src="$1" dest="$2" tmp
  mkdir -p "$(dirname "$dest")"
  # MINTING a brand-new passphrase: only ever allowed when BOTH cipher dirs are
  # PROVEN virgin (absent or empty). unreadable is not "no data" - fail closed.
  # This check runs BEFORE mktemp so a refusal leaves no temp file beside $dest.
  if [ "$src" = "-" ]; then
    cipher_dirs_virgin || die "refusing to generate a new passphrase: a cipher dir ($CIPHER_DIR, $VALKEY_CIPHER_DIR) is not proven empty. List them (sudo ls -la $CIPHER_DIR $VALKEY_CIPHER_DIR); if encrypted data is there restore the passphrase file it was created with, then run scripts/first-run-sidecar.sh again. Nothing was created or changed."
  fi
  tmp="$(umask 077; mktemp "$dest.XXXXXX")" || die "cannot create a temp file next to $dest - check the parent dir is writable and has free disk (ls -ld $(dirname $dest); df -h $dest), then run scripts/first-run-sidecar.sh again."
  if [ "$src" = "-" ]; then
    ( umask 077; openssl rand -base64 32 > "$tmp" ) || { rm -f "$tmp"; die "failed to generate the passphrase file - check openssl is present (command -v openssl) and the target filesystem is writable, then run scripts/first-run-sidecar.sh again. Nothing was created or changed."; }
  else
    cat "$src" > "$tmp" || { rm -f "$tmp"; die "failed to copy the passphrase to $dest - check the source passphrase file is readable (sudo ls -l $src) and the destination filesystem is writable, then run scripts/first-run-sidecar.sh again."; }
  fi
  chmod 0600 "$tmp"
  [ -s "$tmp" ] || { rm -f "$tmp"; die "passfile for $dest would be empty - the store entrypoint refuses an empty passfile. Restore a non-empty passphrase file (mode 0600) from your backup, then run scripts/first-run-sidecar.sh again."; }
  mv -f "$tmp" "$dest" || { rm -f "$tmp"; die "cannot move the passphrase into place at $dest - check the destination dir is writable (sudo ls -ld $(dirname $dest)) and has free disk (df -h), then run scripts/first-run-sidecar.sh again. Nothing was changed; the temp copy was removed."; }
}
if [ -e "$PASS_FILE" ] && [ ! -f "$PASS_FILE" ]; then die "$PASS_FILE exists but is not a regular file. Remove it or restore the passphrase file (mode 0600), then run scripts/first-run-sidecar.sh again."; fi
pass_note=""
if [ "$adopt_run_key" = 1 ]; then
  write_secret "$PASS_FILE" "$PERSIST_FILE"      # no ciphertext yet: the key in use wins
  pass_note="persisted"
elif [ -s "$PERSIST_FILE" ] && [ -s "$PASS_FILE" ]; then
  pass_note="kept"
elif [ -s "$PERSIST_FILE" ]; then
  write_secret "$PERSIST_FILE" "$PASS_FILE"      # reboot path: the /run copy was wiped
  pass_note="restored"
elif [ -s "$PASS_FILE" ]; then
  write_secret "$PASS_FILE" "$PERSIST_FILE"      # older install: key lived only on tmpfs
  pass_note="persisted"
else
  write_secret - "$PERSIST_FILE"                 # first run only (guard above proved no ciphertext)
  write_secret "$PERSIST_FILE" "$PASS_FILE"
  pass_note="generated"
fi

# --- report: paths and modes only, never contents ---------------------------
[ -n "$made_dirs" ] && echo "sidecar: created $made_dirs"
[ -n "$kept_dirs" ] && echo "sidecar: kept (already there, modes untouched):$kept_dirs"
case "$pass_note" in
  kept) echo "sidecar: passfile $PASS_FILE already exists — left byte-identical; compared, never printed." ;;
  restored) echo "sidecar: restored $PASS_FILE from the persistent key file $PERSIST_FILE. The stores read the persistent copy; the /run copy is never read by a container. Value never printed." ;;
  persisted) echo "sidecar: passfile $PASS_FILE already exists - left byte-identical. Saved a persistent copy of the key at $PERSIST_FILE (mode 0600) - until now it lived only on /run, which a reboot wipes. Value never printed." ;;
  generated) echo "sidecar: generated the passphrase into $PERSIST_FILE (mode 0600) and copied it to $PASS_FILE. Its value is never printed and is NOT in .env." ;;
esac
echo "sidecar: versitygw cipher $CIPHER_DIR ($(stat -c %a "$CIPHER_DIR")) | valkey cipher $VALKEY_CIPHER_DIR ($(stat -c %a "$VALKEY_CIPHER_DIR")) | passfile $PASS_FILE ($(stat -c %a "$PASS_FILE")) | persistent key $PERSIST_FILE ($(stat -c %a "$PERSIST_FILE"))"
echo "sidecar: do NOT run 'gocryptfs -init' yourself — the store entrypoint inits once, over a virgin cipher dir only."
echo "back this up (contents never printed here): the PERSISTENT passphrase file $PERSIST_FILE (the $PASS_FILE copy is rebuilt from it) and the gocryptfs.conf inside EACH cipher dir — the versitygw one (e.g. $CIPHER_DIR/gocryptfs.conf) and the Valkey one (e.g. $VALKEY_CIPHER_DIR/gocryptfs.conf). Losing either means that store never starts and its data is unrecoverable."
