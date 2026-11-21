#!/usr/bin/env bash
# scripts/install.sh - thin one-command CE installer.
#   scripts/install.sh [--lan | --no-lan] [--host NAME_OR_IP] [--bind ADDR]
#                      [--keycloak-port N] [--yes] [--no-start] [--no-pull]
#   scripts/install.sh --enable-builder   (only add `builder` to COMPOSE_PROFILES in .env, then exit)
# LAN-reachable is the default: with no flag install.sh detects this computer's
# network address and opens CE to it (bind 0.0.0.0, one printed notice); --no-lan
# keeps it on this computer only. Explicit choices always win; no address = localhost
# Exit codes: 0 success; 1 = fatal error; 2 = usage error.
# (idempotent): it rewrites ONLY the managed block of .env; secrets are kept.
set -eu
umask 077
[ -n "${BASH_VERSION:-}" ] || { echo "install.sh: needs bash. Run it as: bash scripts/install.sh (if bash is missing: sudo apt install bash / apk add bash), then run again." >&2; exit 2; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; OVERLAY="$(dirname "$HERE")"
cd "$OVERLAY"
[ -f docker-compose.yml ] || { echo "install.sh: no docker-compose.yml in $OVERLAY - run scripts/install.sh from the checkout root that holds docker-compose.yml (check: ls docker-compose.yml), or copy the whole open-core/overlay tree, then run again." >&2; exit 2; }
ENV_FILE="$OVERLAY/.env"; B0='# >>> mod-ce install.sh (managed block: change it by re-running scripts/install.sh) >>>'
B1='# <<< mod-ce install.sh <<<'
# FIX-INSTALL-COMPOSE-PROFILES-APPEND: "enable the node builder later" is a SAFE MERGE
# into the ONE COMPOSE_PROFILES line of .env (a second line would win in Compose and
# silently drop the user's list, e.g. `registry`): no line -> appended; empty -> builder;
# list without builder -> <list>,builder in place; already has builder -> file untouched;
# a quoted value keeps its quotes; every other line is byte-identical. The same awk is
# in README.md.tmpl (a test runs both on the same fixtures).
add_builder_profile() {
  local tmp
  [ -f "$ENV_FILE" ] || { : > "$ENV_FILE"; chmod 600 "$ENV_FILE"; }
  tmp="$(mktemp "$ENV_FILE.XXXXXX")" || return 1
  awk 'BEGIN{d=0} /^COMPOSE_PROFILES=/ && !d {d=1; v=substr($0,18); q=""; if (v ~ /^[ ]*["\047].*["\047][ ]*$/) {gsub(/^[ ]+|[ ]+$/,"",v); q=substr(v,1,1); v=substr(v,2,length(v)-2)} gsub(/^[ ]+|[ ]+$/,"",v); n=split(v,a,","); h=0; for(i=1;i<=n;i++){gsub(/^[ ]+|[ ]+$/,"",a[i]); if(a[i]=="builder")h=1} if(h){print; next} sub(/,+$/,"",v); print "COMPOSE_PROFILES=" q (v==""?"builder":v",builder") q; next} {print} END{if(!d) print "COMPOSE_PROFILES=builder"}' "$ENV_FILE" > "$tmp" || { rm -f "$tmp"; return 1; }
  if cmp -s "$tmp" "$ENV_FILE"; then rm -f "$tmp"; echo "install.sh: node builder already in COMPOSE_PROFILES ($ENV_FILE): nothing to change"
  else chmod 600 "$tmp"; mv "$tmp" "$ENV_FILE"; echo "install.sh: added builder to COMPOSE_PROFILES in $ENV_FILE"; fi
}
mode=""; host=""; bind=""; kport=""; yes=0; start=1; pull=1; enable_builder=0
while [ $# -gt 0 ]; do case "$1" in
  --enable-builder) enable_builder=1 ;;
  --lan) mode=lan ;; --no-lan) mode=local ;; --yes|-y) yes=1 ;; --no-start) start=0 ;; --no-pull) pull=0 ;;
  --host) host="${2:?--host needs a name or IP}"; mode=lan; shift ;;
  --bind) bind="${2:?--bind needs an address}"; shift ;;
  --keycloak-port) kport="${2:?--keycloak-port needs a number}"; shift ;;
  -h|--help) sed -n 2,8p "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "install.sh: unknown option $1 (try --help)" >&2; exit 2 ;; esac; shift; done
[ "$enable_builder" = 1 ] && { add_builder_profile; exit $?; }

detect_ip() { # first non-loopback, non-docker IPv4 of the default route, else ""
  local ip=""
  command -v ip >/dev/null 2>&1 && ip="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1)"
  [ -n "$ip" ] || { command -v ipconfig >/dev/null 2>&1 && ip="$(ipconfig getifaddr en0 2>/dev/null || true)"; }
  [ -n "$ip" ] || ip="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^127\.\|^172\.1[7-9]\.\|^172\.2[0-9]\.\|^172\.3[01]\.' | head -n1 || true)"
  # FIX-INSTALL-SH-SIDECAR-FAILURE-GUARD-AND-IPV6 (audit finding 3): an IPv6-only
  #   host has no IPv4 route and `hostname -I` prints nothing, so every probe above
  #   returns "". Reached ONLY when they all did, so a normal (IPv4) host never
  #   takes this path and its answer is byte-identical to before. `ip -6 route get`
  #   to an off-net target names this host's own address in the `src` field; the
  #   value is kept only when it is hex digits and colons (anything else - a name,
  #   an error line - is dropped, exactly as the IPv4 probes drop noise).
  # FIX-INSTALL-SH-SIDECAR-FAILURE-GUARD-AND-IPV6 (audit finding 3): an IPv6-only
  #   host has no IPv4 route and `hostname -I` prints nothing, so every probe above
  #   returns "". Reached ONLY when they all did, so a normal (IPv4) host never
  #   takes this path and its answer is byte-identical to before. `ip -6 route get`
  #   to an off-net target names this host's own address in the `src` field; the
  #   value is kept only when it is hex digits and colons (anything else - a name,
  #   an error line - is dropped, exactly as the IPv4 probes drop noise).
  [ -n "$ip" ] || { ip6=""; command -v ip >/dev/null 2>&1 && ip6="$(ip -6 route get 2001:4860:4860::8888 2>/dev/null | sed -n 's/.* src \([0-9a-fA-F:.]*\).*/\1/p' | head -n1 || true)"
    case "$ip6" in *[!0-9a-fA-F:]*|'') : ;; *) ip="$ip6" ;; esac; }
  printf '%s' "$ip"
}
envget() { grep "^$1=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- || true; }

# 0. FIX-INSTALL-DOCKER-PREFLIGHT (S-inst-5): docker, Compose v2 and the daemon
#    are probed BEFORE any .env or secret is written; a failed probe exits 2
#    having written nothing (no state to recover from). The probes are read-only.
command -v docker >/dev/null 2>&1 || { echo "install.sh: Docker is not installed on this computer. Install Docker (docker-ce plus the Compose v2 plugin; see README Prerequisites), then run scripts/install.sh again." >&2; exit 2; }
# FIX-INSTALL-SH-COMPOSE-VERSION-PROBE-P1: the old probe (`grep -Eq 'version v?2\.'`)
#   matched ONLY a major of 2, so a current Docker (Compose v5.3.1 on r9700) stopped
#   a fresh CE install with rc 2. Now: take the FIRST number after "version" (with
#   or without a leading v) and require it >= 2, so v2, v3 ... v9, v10+ all pass.
#   The old docker-compose v1 standalone and a missing plugin print no version line
#   (or a v1 line), so they still fail here with the SAME message and exit 2.
compose_major="$(docker compose version 2>/dev/null | sed -n 's/.*[Vv]ersion[[:space:]]*[Vv]*\([0-9][0-9]*\).*/\1/p' | head -n1 || true)"
[ -n "$compose_major" ] && [ "$compose_major" -ge 2 ] || { echo "install.sh: this Docker has no Compose v2 (docker compose). Install the compose plugin (Ubuntu: docker-compose-v2; Debian: docker-compose; or Docker's own docker-ce + docker-compose-plugin), then run scripts/install.sh again." >&2; exit 2; }
if ! dinfo_err="$(docker info 2>&1 >/dev/null)"; then
  case "$dinfo_err" in
    *ermission\ denied*) echo "install.sh: this user cannot reach the Docker socket (permission denied). Fix: sudo usermod -aG docker \$USER then newgrp docker (or log out), then run scripts/install.sh again." >&2 ;;
    *) echo "install.sh: Docker is installed but the daemon is not reachable (docker info failed). Start it (sudo systemctl start docker), then run scripts/install.sh again." >&2 ;;
  esac
  exit 2
fi

# 1. secrets first (both are idempotent and print key NAMES only)
# FIX-INSTALL-SH-CHECK-EXIT-CODE-OF-THE-TWO-SECRET-SCRIPTS: a failing secrets script
#   used to be ignored, so the failure only showed up later as a raw compose
#   "required variable ... is missing" message. Stop here instead, name the failing
#   script and the command to re-run it (both print key NAMES only, never values).
sec_rc=0; ./scripts/first-run-secrets.sh || sec_rc=$?
if [ "$sec_rc" != 0 ]; then
  echo "install.sh: STOPPED before starting: the secrets step (scripts/first-run-secrets.sh) failed with exit code $sec_rc, so the passwords and keys are not all generated." >&2
  echo "install.sh: fix the cause named above, then run scripts/install.sh again (safe to repeat; your answers and passwords are kept), or re-run just that step with: ./scripts/first-run-secrets.sh" >&2
  exit 1
fi
sec_rc=0; ./keycloak/ensure-client-secrets.sh || sec_rc=$?
if [ "$sec_rc" != 0 ]; then
  echo "install.sh: STOPPED before starting: the sign-in client secrets step (keycloak/ensure-client-secrets.sh) failed with exit code $sec_rc, so the Keycloak client secrets are not all generated." >&2
  echo "install.sh: fix the cause named above, then run scripts/install.sh again (safe to repeat; your answers and passwords are kept), or re-run just that step with: ./keycloak/ensure-client-secrets.sh" >&2
  exit 1
fi
prev_host="$(envget PUBLIC_HOST)"

# 2. decide the mode (FIX-INSTALL-SH-DEFAULT-BASE-DOMAIN-LAN-ADDRESS-NOT-LOCALHOST:
#    owner rule HEAD-7 - CE must be LAN-reachable with no hand-set variables, so a
#    bare install now DEFAULTS to the detected LAN address (bind 0.0.0.0) and prints
#    one clear notice. An explicit --lan/--no-lan/--host/--bind, a saved .env address,
#    or BASE_DOMAIN/CE_BIND_ADDR set in the environment before the run always win.
#    On a terminal the one question keeps its default Y ([Y/n]). No detectable
#    address = localhost with one printed WARNING.)
if [ -z "$mode" ]; then
  if [ -n "$prev_host" ] && [ "$prev_host" != localhost ]; then mode=lan; host="$prev_host"   # keep current choice
  elif [ -n "${BASE_DOMAIN:-}" ] || [ -n "${CE_BIND_ADDR:-}" ] || [ -n "$bind" ]; then mode=local   # operator set them before the run (--bind alone stays this-computer-only): never auto-open, never override
  elif [ "$yes" = 0 ] && [ -t 0 ] && [ -t 1 ]; then
    guess="$(detect_ip)"; echo "This computer's network address looks like: ${guess:-unknown}"
    printf 'Open this install to other computers on your network? [Y/n] '; read -r a || a=y
    case "$a" in n|N|no|NO) mode=local ;; *) mode=lan ;; esac
  else
    guess="$(detect_ip)"
    # keep only a plain dotted IPv4 (an IPv6 literal or noise must not auto-open)
    case "$guess" in *[!0-9.]*|'') mode=local ;; *) mode=lan; host="$guess" ;; esac
    if [ "$mode" = lan ]; then
      echo "install.sh: CE will be reachable from other computers on your network at http://$host:3000. Re-run with --no-lan to keep it on this computer only."
    else
      echo "install.sh: WARNING: could not detect this computer's LAN address; CE will only be reachable from this computer. Re-run with --lan --host <ip> to change that." >&2
    fi
  fi
fi

# 3. validate and derive (the host lands in .env and in Keycloak: strict charset)
if [ "$mode" = lan ]; then
  [ -n "$host" ] || host="$(detect_ip)"
  [ -n "$host" ] || { echo "install.sh: could not detect a LAN address; pass --host <ip-or-name>" >&2; exit 2; }
  # FIX-INSTALL-SH-SIDECAR-FAILURE-GUARD-AND-IPV6 (audit findings 3 and 6): the
  #   validation below rejects every colon, so an IPv6 address (detected or typed)
  #   used to get the dead-end "unusable --host". First classify: a value made of
  #   hex digits and colons ONLY, with a colon, is an IPv6 literal.
  ipv6=0
  case "$host" in *[!0-9a-fA-F:]*) : ;; *:*) ipv6=1 ;; esac
  # DECISION (smaller safe option, see the result file): an IPv6 literal is now
  #   RECOGNISED but REFUSED with an actionable message, BEFORE .env, Keycloak or
  #   any compose action - install.sh builds URLs, Keycloak host names and cookie
  #   domains from this value, and a bracket-free IPv6 literal is ambiguous in
  #   `http://host:port`; bracketing it everywhere would change what Keycloak,
  #   the Next.js dashboard and MinIO accept, which this change cannot prove.
  if [ "$ipv6" = 1 ]; then
    echo "install.sh: this computer's LAN address is IPv6 ($host); CE's --host must be an IPv4 address or a name (the value lands in URLs, the Keycloak host name and cookie domains, which do not take a bracket-free IPv6 literal). Pass --host <ipv4-or-name>, or run without --lan for localhost-only." >&2
    exit 2
  fi
  case "$host" in localhost|127.*|0.0.0.0|*[!A-Za-z0-9.-]*|-*|.*) echo "install.sh: unusable --host '$host' (letters/digits/./- only, not starting with -). Pass --host <ipv4-or-name> and run scripts/install.sh again." >&2; exit 2 ;; esac
  if [ -z "$bind" ] && [ -n "$prev_host" ] && [ "$prev_host" != localhost ]; then bind="$(envget CE_BIND_ADDR)"; fi; : "${bind:=0.0.0.0}"
else host=localhost; [ -z "$bind" ] || echo "install.sh: --bind ignored without --lan" >&2; bind=127.0.0.1; fi
: "${kport:=$(envget KEYCLOAK_HOST_PORT)}"; : "${kport:=8080}"
case "$kport" in *[!0-9]*|'') echo "install.sh: bad keycloak port - pass --keycloak-port <number 1-65535> and run scripts/install.sh again." >&2; exit 2 ;; esac
case "$bind" in *[!0-9.]*|''|.*|*.|*..*) echo "install.sh: bad bind address - pass --bind <dotted-ipv4> together with --lan, then run scripts/install.sh again." >&2; exit 2 ;; esac

# 4. rewrite ONLY the managed block (atomic; keys outside the block are never touched)
if [ -f "$ENV_FILE" ]; then
  n0="$(grep -cxF -- "$B0" "$ENV_FILE" || true)"; n1="$(grep -cxF -- "$B1" "$ENV_FILE" || true)"
  { [ "$n0" = "$n1" ] && [ "$n0" -le 1 ]; } || { echo "install.sh: the managed-block marker lines in .env are unbalanced; nothing was changed. Find them with: grep -n 'mod-ce install.sh' .env, fix or delete those lines, then run scripts/install.sh again." >&2; exit 2; }
fi
tmp="$(mktemp "$OVERLAY/.env.XXXXXX")"
if [ -f "$ENV_FILE" ]; then
  awk -v a="$B0" -v z="$B1" '$0==a{s=1;next} $0==z{s=0;next} !s' "$ENV_FILE" > "$tmp" || { rm -f "$tmp"; echo "install.sh: could not rewrite .env; nothing was changed. Check free disk and that .env is writable (df -h .; ls -l .env), then run scripts/install.sh again." >&2; exit 1; }
fi
if [ "$mode" = lan ] || [ "$kport" != 8080 ]; then {
  echo "$B0"
  [ "$mode" = lan ] && cat <<EOF
PUBLIC_HOST=$host
CE_BIND_ADDR=$bind
BASE_DOMAIN=$host
API_BASE_DOMAIN=$host:3000
KC_HOSTNAME=$host
KEYCLOAK_HOSTNAME=$host
DASHBOARD_URL=http://$host:3000
MOD_DASHBOARD_URL=http://$host:3000
MOD_API_URL=http://$host:8000/v1
MINIO_PUBLIC_ENDPOINT=$host:7070
NEXT_PUBLIC_API_URL=http://$host:8000/v1
NEXT_PUBLIC_DASHBOARD_URL=http://$host:3000
EOF
  printf 'KEYCLOAK_HOST_PORT=%s\nKEYCLOAK_EXTERNAL_URL=http://%s:%s\n' "$kport" "$host" "$kport"
  echo "$B1"; } >> "$tmp"; fi
chmod 600 "$tmp"; mv "$tmp" "$ENV_FILE"
[ -n "$prev_host" ] && [ "$prev_host" != "$host" ] && echo "install.sh: address changed $prev_host -> $host"
for k in BASE_DOMAIN KC_HOSTNAME KEYCLOAK_EXTERNAL_URL DASHBOARD_URL MOD_API_URL; do   # hand-set copies outside the block
  [ "$mode" = lan ] && awk -v a="$B0" -v z="$B1" -v k="$k" '$0==a{s=1} $0==z{s=0} !s && index($0,k"=")==1{f=1} END{exit !f}' "$ENV_FILE" \
    && echo "install.sh: note: $k is also set outside the managed block; the block (later in the file) wins"
done

# 4b. FIX-INSTALL-ENABLE-NODE-BUILDER-DEFAULT (the node builder is
#     ON by default on day 1). `buildkitd-net` and `buildkitd` carry
#     `profiles: ["builder"]` (services buildkitd-net and buildkitd in docker-compose.yml), so compose starts
#     them ONLY when COMPOSE_PROFILES says so - and nothing in CE ever set it,
#     so a fresh install never started the builder. Write COMPOSE_PROFILES=builder
#     OUTSIDE the managed block (a hand-set key like the others: install.sh
#     writes it once and never rewrites it), BEFORE the --no-start exit so the
#     line lands on every install, started or not. Rules: a value the user
#     already set (in the environment when install.sh runs, OR in .env) is left
#     EXACTLY as is - never clobber a user's choice; a user value that does not
#     contain `builder` gets one INFO line naming the fix; only when nothing is
#     set anywhere is `COMPOSE_PROFILES=builder` written. Idempotent: a second
#     run sees the line and writes nothing. The other opt-in profiles
#     (stack-ingress, registry) stay opt-in - they are never added here.
profiles_env="${COMPOSE_PROFILES:-}"; builder_on=0; builder_written=0; builder_skip=0
[ -n "$profiles_env" ] || profiles_env="$(envget COMPOSE_PROFILES)"
if [ -n "$profiles_env" ]; then
  case ",${profiles_env}," in *",builder,"*) builder_on=1 ;; # builder already enabled: nothing to say
    *) echo "install.sh: INFO: the node builder (the wizard builds nodes with it) is NOT enabled: COMPOSE_PROFILES is set to $profiles_env without builder. Enable it by adding it: COMPOSE_PROFILES=$profiles_env,builder in $ENV_FILE" ;;
  esac
elif [ -f "$ENV_FILE" ] && grep -q '^COMPOSE_PROFILES=' "$ENV_FILE"; then
  : # hand-set with an empty value: the user's choice, left as is
else
  printf 'COMPOSE_PROFILES=builder\n' >> "$ENV_FILE"; builder_on=1; builder_written=1
  echo "install.sh: node builder enabled by default (COMPOSE_PROFILES=builder in $ENV_FILE)"
fi

# 5. start (compose recreates only containers whose config changed; data volumes are kept)
[ "$start" = 1 ] || { echo "install.sh: --no-start: .env written, nothing started"; exit 0; }
# 5a. the sidecar host paths need one sudo line on a first install: stop right here
# (do not pull or start anything), print that line, and let the user re-run us.
# first-run-secrets.sh (step 1) already ran the same sidecar and only WARNs on
# failure, so on a first install the sudo line can appear twice: the sidecar is
# idempotent and writes nothing on the sudo path. The stop here is keyed on the
# exit code AND on the sudo line actually being in the output, so a sidecar that
# fails for another reason never stops the install on a bare exit code - until
# FIX-INSTALL-SH-SIDECAR-FAILURE-GUARD-AND-IPV6 (the guard below).
sc_rc=0; sc_out="$(bash ./scripts/first-run-sidecar.sh 2>&1)" || sc_rc=$?
if [ -f ./scripts/first-run-sidecar.sh ] && [ "$sc_rc" != 0 ] \
  && printf '%s\n' "$sc_out" | grep -q 'sudo bash '; then
  printf '%s\n' "$sc_out"
  echo "install.sh: STOPPED before starting: the encrypted-storage folders need one command with sudo (the line printed above)." >&2
  echo "install.sh: run that line, then run scripts/install.sh again (safe to repeat; your answers and passwords are kept)." >&2
  exit 1
fi
# FIX-INSTALL-SH-SIDECAR-FAILURE-GUARD-AND-IPV6 (audit finding 1, BLOCKER): the sudo
#   path above is UNCHANGED (same key: exit code AND the sudo line actually in the
#   output). Everything that got past it is a sidecar failure that is NOT "needs root"
#   - a missing scripts/lib/ce-host-resources.sh, a bad CE_TENANT_SLUG, a missing
#   overlay file, an I/O error. Before this fix the exit code was dropped there and the
#   install went on to pull and start WITHOUT the cipher dirs, so the stores failed
#   later with the opaque "encrypted mount is not up". Now: print the captured error
#   (first-run-sidecar.sh prints key NAMES only, never values, so this cannot leak a
#   secret) and exit 1 BEFORE any compose action.
if [ -f ./scripts/first-run-sidecar.sh ] && [ "$sc_rc" != 0 ]; then
  printf '%s\n' "$sc_out"
  echo "install.sh: STOPPED before starting: the encrypted-storage step (scripts/first-run-sidecar.sh) failed with exit code $sc_rc and printed no sudo line, so the encrypted-storage folders are NOT set up (a later run would fail with 'encrypted mount is not up')." >&2
  echo "install.sh: fix the cause named above, then run scripts/install.sh again (safe to repeat; your answers and passwords are kept)." >&2
  exit 1
fi
# 5a3. FIX-CE-REBOOT-RECOVERY-ENCSTORE-UNIT-BEFORE-DOCKER-AND-DIR-GUARD: the boot
#     unit that restores the encrypted-store key BEFORE dockerd restores the containers
#     (plus the docker.service drop-in that orders Docker after it). Without it a reboot
#     leaves my_redis/my_versitygw Exited (127) and the stack unhealthy until someone
#     runs the recover script. Idempotent: install -m 644 over an existing copy is a
#     no-op, daemon-reload + enable are safe to repeat. Guarded by `command -v
#     systemctl` (a host without systemd - WSL2 without a systemd PID 1, containers -
#     just skips it and prints the exact commands). Never interactive sudo: only euid 0
#     or `sudo -n true`; otherwise the one-line command is printed and install CONTINUES
#     (this never fails an install; the recover-after-reboot unit still fixes a reboot).
if command -v systemctl >/dev/null 2>&1 && [ -f ./scripts/mod-ce-encstore.service ] && [ -f ./scripts/docker.service.d-90-encstore.conf ]; then
  # the unit's ExecStart names the checkout root that holds scripts/ (OVERLAY is that
  # root: install.sh runs from it and checks ./scripts/first-run-sidecar.sh the same way).
  ce_root="$OVERLAY"
  enc_install_cmd="sudo install -m 644 $OVERLAY/scripts/mod-ce-encstore.service /etc/systemd/system/ && sudo install -m 644 $OVERLAY/scripts/docker.service.d-90-encstore.conf /etc/systemd/system/docker.service.d/90-encstore.conf && sudo systemctl daemon-reload && sudo systemctl enable mod-ce-encstore.service"
  enc_unit_tmp="$(mktemp 2>/dev/null || echo /dev/null)"
  if [ "$enc_unit_tmp" != /dev/null ] && sed "s|/CHANGE_ME/scripts/|$ce_root/scripts/|" ./scripts/mod-ce-encstore.service > "$enc_unit_tmp" 2>/dev/null; then
    if [ "$(id -u)" = 0 ]; then
      install -m 644 "$enc_unit_tmp" /etc/systemd/system/mod-ce-encstore.service 2>/dev/null || true
      mkdir -p /etc/systemd/system/docker.service.d 2>/dev/null || true
      install -m 644 ./scripts/docker.service.d-90-encstore.conf /etc/systemd/system/docker.service.d/90-encstore.conf 2>/dev/null || true
      systemctl daemon-reload 2>/dev/null || true
      systemctl enable mod-ce-encstore.service 2>/dev/null || true
      echo "install.sh: encrypted-store boot unit installed (mod-ce-encstore.service runs BEFORE Docker, so a reboot restores the key copy before the stores are re-created)."
    elif sudo -n true >/dev/null 2>&1; then
      sudo install -m 644 "$enc_unit_tmp" /etc/systemd/system/mod-ce-encstore.service 2>/dev/null || true
      sudo mkdir -p /etc/systemd/system/docker.service.d 2>/dev/null || true
      sudo install -m 644 ./scripts/docker.service.d-90-encstore.conf /etc/systemd/system/docker.service.d/90-encstore.conf 2>/dev/null || true
      sudo systemctl daemon-reload 2>/dev/null || true
      sudo systemctl enable mod-ce-encstore.service 2>/dev/null || true
      echo "install.sh: encrypted-store boot unit installed (mod-ce-encstore.service runs BEFORE Docker, so a reboot restores the key copy before the stores are re-created)."
    else
      echo "install.sh: the encrypted-store boot unit was NOT installed (this user cannot run sudo without a prompt). To make a reboot bring the encrypted stores back on its own, run exactly:"
      echo "  $enc_install_cmd"
    fi
  fi
  [ -f "$enc_unit_tmp" ] && rm -f "$enc_unit_tmp"
fi
# 5a2. FIX-INSTALL-ENABLE-NODE-BUILDER-DEFAULT: the AppArmor profile the rootless
#     BuildKit sidecar needs (the node builder). Run it only when it can run
#     WITHOUT a prompt: effective uid 0, or `sudo -n true` succeeds (never an
#     interactive sudo that could hang the install). If it cannot run, print the
#     EXACT one-line command and CONTINUE - this never fails the install. The
#     script is idempotent (install -m 0644 + apparmor_parser -r, persisted in
#     /etc/apparmor.d) and no-ops with exit 0 on hosts without AppArmor or
#     without the userns gate: that no-op's own message is PRINTED here (it carries
#     the CE_BUILDKIT_APPARMOR=unconfined fix-line; its check is reused, never duplicated).
aa_rc=99; aa_out=""
if [ -f ./scripts/install-buildkit-apparmor.sh ]; then
  if [ "$(id -u)" = 0 ]; then
    if aa_out="$(bash ./scripts/install-buildkit-apparmor.sh 2>&1)"; then aa_rc=0; else aa_rc=$?; fi
  elif sudo -n true >/dev/null 2>&1; then
    if aa_out="$(sudo -n bash ./scripts/install-buildkit-apparmor.sh 2>&1)"; then aa_rc=0; else aa_rc=$?; fi
  fi
fi
# BUILDER-DEFAULT-REV-FIXES F2: when the profile is NOT installed (no root and no
# passwordless sudo, or the script failed) compose would still try buildkitd
# (security_opt apparmor=mod-buildkitd needs the loaded profile) and `set -eu`
# would abort the whole install. So the builder is NOT enabled for THIS run: the
# `.env` line install.sh itself wrote is removed again (a later bare
# `docker compose up -d` stays safe, and re-running install.sh after the
# profile is installed writes it once more), a value the USER set is left alone
# and only stripped of `builder` for the one `up` below. The install exits 0;
# the node builder is then "not installed", not a failure.
builder_enable_cmd="cd \"$OVERLAY\" && sudo bash $OVERLAY/scripts/install-buildkit-apparmor.sh && $OVERLAY/scripts/install.sh --enable-builder && docker compose --profile builder up -d buildkitd-net buildkitd"
case "$aa_out" in
  # UD3 (row 7): both no-op lines carry a real fix-line (CE_BUILDKIT_APPARMOR=unconfined): print them.
  *'AppArmor is not active'*|*'does not gate user namespaces'*) printf '%s\n' "$aa_out" ;;
  *) if [ "$aa_rc" = 99 ] && [ ! -f ./scripts/install-buildkit-apparmor.sh ]; then
       # UD4 (row 11): aa_rc=99 is also the value when the script is absent; name that cause only.
       echo "install.sh: the node builder's AppArmor profile script (scripts/install-buildkit-apparmor.sh) is missing from this checkout: continuing without the node builder."
       builder_skip=1
     elif [ "$aa_rc" = 99 ]; then
       echo "install.sh: the node builder's AppArmor profile was NOT installed (no root, no passwordless sudo): continuing without the node builder. To enable it later run (safe to repeat):"
       echo "  $builder_enable_cmd"
       builder_skip=1
     elif [ "$aa_rc" != 0 ]; then
       printf '%s\n' "$aa_out"
       echo "install.sh: the node builder's AppArmor profile could not be installed (exit $aa_rc): continuing without the node builder. To enable it later fix the error above, then run (safe to repeat):"
       echo "  $builder_enable_cmd"
       builder_skip=1
      # aa_rc=0 (success): print the script's own output again, but NOT the two no-op
      # wordings - the case arm above already printed those with their fix-line, so a
      # mutant that drops one of those alternatives makes B6d red instead of silently
      # re-printing the line here.
      elif [ -n "$aa_out" ]; then case "$aa_out" in *'AppArmor is not active'*|*'does not gate user namespaces'*) : ;; *) printf '%s\n' "$aa_out" ;; esac
     fi ;;
esac
up_profiles="$profiles_env"
if [ "$builder_skip" = 1 ]; then
  if [ "$builder_written" = 1 ]; then
    tmp_env="$(mktemp)"; grep -vx 'COMPOSE_PROFILES=builder' "$ENV_FILE" > "$tmp_env" || true
    cat "$tmp_env" > "$ENV_FILE"; rm -f "$tmp_env"
  fi
  up_profiles="$(printf '%s' "$profiles_env" | tr ',' '\n' | grep -vx builder | paste -sd, - || true)"
fi
# 5b-pre. FIX-INSTALL-LOW-DISK-CHECK (S-inst-3): free-space probe BEFORE any
# download. Runs only on the pull path (--no-pull downloads nothing, --no-start
# already exited above), so a healthy install never pays for it. The filesystem
# checked is docker's root (`docker info` answers it; if docker does not answer,
# the overlay dir, which is where the compose volumes live anyway). `df -Pk` is
# POSIX (KiB) - never GNU-only --output. If the probe cannot run (no df, empty
# parse) it is SKIPPED silently: it must never block a healthy install.
# INSTALL_MIN_FREE_KB is a plain shell variable, NOT an environment knob: the
# images are ~9 GB on disk (measured) plus docker's working buffer -> 20 GiB of free space.
INSTALL_MIN_FREE_KB=20971520
if [ "$pull" = 1 ]; then
  droot="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
  [ -n "$droot" ] || droot="$OVERLAY"
  free_kb="$(df -Pk "$droot" 2>/dev/null | awk 'NR==2{print $4}')"
  case "$free_kb" in (''|*[!0-9]*) : ;; # probe unavailable: skipped silently
    *) if [ "$free_kb" -lt "$INSTALL_MIN_FREE_KB" ]; then
      echo "install.sh: NOT ENOUGH FREE DISK on the filesystem holding docker's images ($droot): $((free_kb / 1048576)) GiB free, at least 20 GiB needed (the images are ~9 GB plus docker's working space)." >&2
      echo "install.sh: free disk there (or point docker at a bigger filesystem), then run scripts/install.sh again (safe to re-run; your answers and passwords are kept); or load/build the images yourself and run: scripts/install.sh --no-pull" >&2
      exit 1
    fi ;;
  esac
fi
# 5b. images: pull; if the pull fails but every image is already on this computer, carry on
if [ "$pull" = 0 ]; then
  # --no-pull: verify the images really are on this computer before starting
  imgs="$(docker compose config --images 2>/dev/null || true)"; missing=""
  for img in $imgs; do docker image inspect "$img" >/dev/null 2>&1 || missing="$missing $img"; done
  if [ -n "$missing" ]; then
    echo "install.sh: --no-pull: these images are not on this computer:$missing" >&2
    echo "install.sh: load/build them (or drop --no-pull) and run again: scripts/install.sh --no-pull" >&2
    exit 1; fi
  [ -n "$imgs" ] || echo "install.sh: --no-pull: no images could be listed (docker or the Compose v2 plugin may be missing - check docker compose version); docker compose up -d will report any image that is missing" >&2
  echo "install.sh: --no-pull: using the images already on this computer"
else
  # FIX-INSTALL-LOW-DISK-CHECK: the pull's STDERR goes to a temp file so a
  # disk-full failure gets the right advice (the result's Fix 2 sketch tested
  # `echo "$?"` for the words - wrong: that is an exit code, not the output).
  pull_err="$(mktemp "${OVERLAY}/.pull-err.XXXXXX" 2>/dev/null || echo /dev/null)"
  # FIX-INSTALL-NO-INTERNET-ADVICE (S-inst-4 row 8): bound the pull. A registry
  # that RESOLVES but is unreachable (captive portal, blocked IP) makes docker
  # retry every image with its own backoff: unbounded that is minutes of silence.
  # `timeout` is coreutils (absent on stock macOS) -> use it only when present,
  # else keep the old unbounded behaviour with no error. The bound is a WALL
  # bound for the WHOLE pull, not per image, and it is deliberately generous:
  # README "What to expect" measures a real working pull (~9 GB) at ~43 min
  # on a slow clean-room install, so the bound must sit ABOVE that (3600 s =
  # 1 h) - a slow-but-working pull of the large CE images always completes, and
  # only a truly hung/unreachable registry (docker's own retries fail in
  # minutes, a dead network in seconds) is cut. A killed pull needs no new
  # branch: the image-inspect loop below still names what is missing and exits
  # 1 with the offline-route advice.
  pull_to=""; if command -v timeout >/dev/null 2>&1; then pull_to="timeout 3600 "; fi
  # UNSAFE-DEVNULL UD1: the fallback above is /dev/null; never rm it (root rm -f deletes the device node).
  # FIX-INSTALL-LOW-DISK-CHECK C2: the pull's progress reaches the terminal (tee) while its
  # error text is captured in $pull_err. `wait` lets the tee flush before the file is read;
  # the pull's own exit status is the if-condition's (process substitution does not change it).
  if ${pull_to}docker compose pull 2> >(tee "$pull_err" >&2); then wait; [ "$pull_err" = /dev/null ] || rm -f "$pull_err"
  else
  wait   # the error text was already shown live by the tee, so no second cat here
  # FIX-INSTALL-LOW-DISK-CHECK C1: the disk-full advice is printed only inside the missing
  # branch below (when images really are missing); a disk-full failure with every image
  # already local falls through to the tolerance and continues.
  disk_full=0
  if grep -qi 'no space left on device' "$pull_err" 2>/dev/null; then disk_full=1; fi
  [ "$pull_err" = /dev/null ] || rm -f "$pull_err"
  imgs="$(docker compose config --images 2>/dev/null || true)"; missing=""
  [ -n "$imgs" ] || missing=" (could not list the images)"
  for img in $imgs; do docker image inspect "$img" >/dev/null 2>&1 || missing="$missing $img"; done
  if [ -n "$missing" ]; then
    if [ "$disk_full" = 1 ]; then
      echo "install.sh: the image download failed because the disk is full (no space left on device)." >&2
      echo "install.sh: free disk space on the filesystem holding docker's images, then run scripts/install.sh again (safe to re-run; your answers and passwords are kept); or load/build the images yourself and run: scripts/install.sh --no-pull" >&2
      exit 1
    fi
    if [ "$imgs" = "" ]; then echo "install.sh: the image download failed and the image list could not be read; load/build the images and run: scripts/install.sh --no-pull" >&2; else echo "install.sh: the image download failed and these images are not on this computer:$missing" >&2; fi
    echo "install.sh: no internet or the registry is unreachable (the CE images are public on ghcr.io/modtim - no login needed by default). Fix the network, or point at a mirror (CE_IMAGE_REGISTRY in .env + docker login - see open-core/overlay/README.md, 'Choosing images'), then run scripts/install.sh again (safe to re-run; your answers and passwords are kept); or build the images from source (README: docker compose -f docker-compose.yml -f docker-compose.dev-build.yml up --build) and run: scripts/install.sh --no-pull" >&2
    echo "install.sh: fix the network or registry login (or install Docker's Compose v2 plugin - check docker compose version) and run again; or load/build those images yourself and run: scripts/install.sh --no-pull" >&2
    exit 1; fi
  echo "install.sh: the image download failed, but every image is already on this computer; continuing without it"
  fi
fi
# 5c. FIX-INSTALL-PORT-CHECKS (S-inst-2 fix 4): PRE-UP GATE - a required host
#     port already taken means `docker compose up -d` fails LATE with raw docker
#     text ("address already in use") and starts what it can first. Fail early,
#     name the ports, touch nothing.
#     OWN-CONTAINER GUARD (required by the brief): on a RE-RUN over an existing
#     install this very project's containers legitimately hold 3000/8000/... -
#     those must NOT be reported. The guard is the compose project label, the
#     same one ce-doctor.sh uses (:144): every container this compose file
#     started carries `com.docker.compose.project=<the name: in docker-compose
#    .yml>` (here `mod-core-ce`), so `docker ps -a --filter
#     label=com.docker.compose.project=<project> --format '{{.Ports}}'` lists
#     exactly the host ports OUR stack published. A port held only by those
#     containers is skipped; a port held by anything else (another app, a
#     foreign stack, a dev server) is reported.
#     FIX-INSTALL-PORT-CHECKS-REV-FIXES (review concern 4): what the code ACTUALLY
#     does when docker is missing/unreadable is yield an EMPTY own_ports list - the
#     scan still runs and every port some foreign process holds is REPORTED (exit 1
#     before `compose up`); only the own-container guard is unavailable then.
#     FIX-INSTALL-PORT-CHECKS-REV-FIXES (review concern 3): a missing `ss` is a
#     VISIBLE skip: install.sh prints the same note first-run-secrets.sh prints for
#     the same case, then proceeds.
compose_project() { # COMPOSE_PROJECT_NAME (env, then .env), else the `name:` in docker-compose.yml, else docker's dir-name default
  # FIX-INSTALL-PORT-CHECKS-REV-FIXES (review concern 2): compose HONOURS
  # COMPOSE_PROJECT_NAME (the environment wins, then .env) - a stack started under
  # a custom project name carries THAT label, so the own-container guard must use
  # the same name or a re-run over that stack reads as a port clash. Same env-then-
  # .env precedence install.sh already uses for COMPOSE_PROFILES (:110).
  local n; n="${COMPOSE_PROJECT_NAME:-}"; [ -n "$n" ] || n="$(envget COMPOSE_PROJECT_NAME)"
  [ -n "$n" ] || n="$(grep -m1 '^name:' docker-compose.yml 2>/dev/null | cut -d: -f2- | tr -d ' ')"
  [ -n "$n" ] || n="$(basename "$OVERLAY")"
  printf '%s' "${n:-mod-core-ce}"
}
own_ports() { # host ports published by THIS project's containers (running or not)
  local p; p="$(compose_project)"
  docker ps -a --format '{{.Ports}}' --filter "label=com.docker.compose.project=$p" 2>/dev/null \
    | tr ',' '\n' | grep -o '[0-9]\+->' | grep -o '[0-9]\+' | sort -u
}
free_port_check() {
  # FIX-INSTALL-PORT-CHECKS-REV-FIXES (review concern 3): the skip is VISIBLE -
  # the same note first-run-secrets.sh prints for the same case (:86), then proceed.
  command -v ss >/dev/null 2>&1 || { echo "install.sh: WARN: no ss on this system — host-port check skipped; check with lsof -i -n -P before docker compose up" >&2; return 0; }
  local p bad="" ours
  ours="$(own_ports)"
  for p in 3000 3001 3002 5432 6379 7070 8000 8200 8025 "$kport"; do
    ss -ltnH "sport = :$p" 2>/dev/null | grep -q . || continue
    printf '%s\n' "$ours" | grep -q "^${p}$" && continue   # held by OUR stack: fine on a re-run
    bad="$bad $p"
  done
  [ -n "$bad" ] || return 0
  echo "install.sh: these host ports are already in use:$bad" >&2
  echo "install.sh: stop that process and run scripts/install.sh again; Keycloak can move with --keycloak-port N." >&2
  return 1
}
free_port_check || exit 1
# FIX-INSTALL-RERUN-UPGRADE-GUARDS (fix 2): the read-only PostgreSQL major-version
# guard runs BEFORE compose up can touch the data volume. A refusal (rc 1), an
# undecidable state (rc 3) or any other non-zero rc stops install here with nothing
# changed. Fresh install (no volume) passes inside the guard; a missing guard script
# is skipped (older checkouts).
if [ -f ./scripts/pg-version-guard.sh ]; then
  pg_rc=0; ./scripts/pg-version-guard.sh -f docker-compose.yml || pg_rc=$?
  if [ "$pg_rc" -ne 0 ]; then
    echo "install.sh: the database-version check did not pass (rc=$pg_rc, see above); nothing was started." >&2
    echo "install.sh: for a PostgreSQL major-version change follow docs/postgres-major-version.md (dump first, then restore), then run scripts/install.sh again." >&2
    exit "$pg_rc"
  fi
fi
# FIX-INSTALL-RERUN-UPGRADE-GUARDS (fix 1): a name/port clash with a container this
# project does NOT own gets the old-stack advice, not the raw compose error. A clash
# with THIS project's own container (re-run) is passed through unchanged, no advice.
# Reuses own_ports() and compose_project() from FIX-INSTALL-PORT-CHECKS above.
up_is_foreign_conflict() { # $1 = compose stderr file; 0 = the clash names something not ours
  local p nm port
  p="$(compose_project)"
  nm="$(sed -n 's/.*container name "\/\{0,1\}\([^"]*\)" is already in use.*/\1/p' "$1" | head -1)"
  if [ -n "$nm" ]; then
    [ -n "$(docker ps -a -q --filter "name=^/?${nm}\$" --filter "label=com.docker.compose.project=$p" 2>/dev/null)" ] && return 1
    return 0
  fi
  port="$(sed -n 's/.*Bind for [^ ]*:\([0-9]*\) failed.*/\1/p' "$1" | head -1)"
  if [ -n "$port" ]; then
    printf '%s\n' "$(own_ports)" | grep -q "^${port}$" && return 1
  fi
  return 0
}
# The builder-default change (FIX-INSTALL-ENABLE-NODE-BUILDER-DEFAULT, upstream) decides
# COMPOSE_PROFILES for this one call; kept as its own function so the stderr capture below
# can wrap it unchanged.
up_cmd() {
  if [ "$builder_skip" = 1 ]; then COMPOSE_PROFILES="$up_profiles" docker compose up -d; else docker compose up -d; fi
}
# 5c-pre. FIX-INSTALL-KEYCLOAK-ADMIN-INIT-RERUN (Tim item 82): remember the
#   Keycloak container's start time BEFORE `up -d`. The README reset path
#   (`docker compose rm -sfv keycloak`, README:321-338) deletes it, so `up -d`
#   gives it a NEW StartedAt - that is the signal that the PREVIOUS run's
#   exited-0 `my_keycloak_admin_init` no longer describes this Keycloak.
#   Empty on a fresh install (no container yet), which is NOT a reset.
kc_prev="$(docker inspect -f '{{.State.StartedAt}}' my_keycloak_server 2>/dev/null || true)"
up_err="$(mktemp "${OVERLAY}/.up-err.XXXXXX" 2>/dev/null || echo /dev/null)"
# FIX-INSTALL-TEST-FINDINGS-OPENBAO-UNSEAL-RACE (gold install test 11-19): on a
# brand-new machine a service can turn healthy a few seconds AFTER compose gave
# up ("dependency failed to start: container ... is unhealthy"); a plain re-run
# then succeeds. Retry `up -d` (bounded: 2 extra tries) ONLY for that one message.
up_try=1
up_ok=0
while :; do
  if up_cmd 2>"$up_err"; then up_ok=1; break; fi
  if [ "$up_try" -lt 3 ] && [ "$up_err" != /dev/null ] && grep -qi 'is unhealthy' "$up_err" 2>/dev/null; then
    cat "$up_err" >&2
    echo "install.sh: First start is slow on a new machine; retrying..." >&2
    up_try=$((up_try + 1)); sleep 10; continue
  fi
  break
done
if [ "$up_ok" = 1 ]; then [ "$up_err" = /dev/null ] || rm -f "$up_err"
else
  [ -s "$up_err" ] && cat "$up_err" >&2
  if grep -qi 'container name .*is already in use\|address already in use\|port is already allocated' "$up_err" 2>/dev/null && up_is_foreign_conflict "$up_err"; then
    [ "$up_err" = /dev/null ] || rm -f "$up_err"
    echo "install.sh: another MOD/CE stack already owns these container names or ports on this computer." >&2
    echo "install.sh: find it with: docker ps -a --filter name=my_ --filter name=mod_ --filter name=workflow_builder; stop/remove the OLD one from ITS checkout (docker compose down there), then run scripts/install.sh again (safe to re-run; your answers and passwords are kept)." >&2
    exit 1
  fi
  [ "$up_err" = /dev/null ] || rm -f "$up_err"; exit 1
fi
# 5d. FIX-INSTALL-ENABLE-NODE-BUILDER-DEFAULT: with COMPOSE_PROFILES=builder in
#     .env the bare `docker compose up -d` above already started buildkitd-net +
#     buildkitd (compose reads COMPOSE_PROFILES from the .env next to the
#     compose file; install.sh passes no explicit --profile/service list). Report
#     it: informational only, the exit code never changes.
node_builder_status() {
  [ "$builder_skip" = 1 ] && { echo "install.sh: node builder: not installed (AppArmor profile missing; the enable-later command is printed above)"; return 0; }
  case ",${profiles_env:-builder}," in *",builder,"*) : ;; *) echo "install.sh: node builder: not enabled (COMPOSE_PROFILES=$profiles_env)" ; return 0 ;; esac
  if docker compose --profile builder ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx buildkitd; then
    echo "install.sh: node builder: running"
  else
    echo "install.sh: node builder: NOT running (see: cd \"$OVERLAY\" && docker compose --profile builder ps buildkitd)"
  fi
}
node_builder_status
echo "install.sh: waiting for the one-time Keycloak setup to finish ..."
# 5c. FIX-INSTALL-KEYCLOAK-ADMIN-INIT-RERUN (Tim item 82): today's loop accepted
#   the PREVIOUS run's `my_keycloak_admin_init` (still `exited 0` ON DISK) as
#   success, so after the README reset path (README:321-338, `docker compose rm
#   -sfv keycloak`) it waited 0 s and the fresh Keycloak never got its permanent
#   admin. RESET signal = the Keycloak container got a NEW StartedAt this run
#   (the `kc_prev` stamp above). Then re-run the ONE-SHOT ONLY
#   (`--force-recreate keycloak-admin-init`: never the other services, never the
#   data volume) and wait for THIS run's exit 0 - the container's FinishedAt must
#   differ from the stamp taken before the recreate. The one-shot is idempotent
#   (keycloak/permanentize-admin.sh:28-29, :123-125 "already permanent ... exit
#   0", :78-79 realm policy "leaving it alone"), so re-running it against an
#   initialised Keycloak cannot duplicate users/clients/realm. A normal re-run
#   over a healthy stack keeps today's behaviour: exited 0 accepted, no recreate.
init_state() { docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' my_keycloak_admin_init 2>/dev/null || true; }
init_finished() { docker inspect -f '{{.State.FinishedAt}}' my_keycloak_admin_init 2>/dev/null || true; }
kc_reset=0
st="$(init_state)"
kc_now="$(docker inspect -f '{{.State.StartedAt}}' my_keycloak_server 2>/dev/null || true)"
if [ -n "$kc_prev" ] && [ "$kc_prev" != "$kc_now" ]; then
  kc_reset=1
  echo "install.sh: Keycloak was reset since the last run; re-running the one-time setup ..."
  init_fin="$(init_finished)"
  docker compose up -d --force-recreate keycloak-admin-init
  for _ in $(seq 1 90); do
    st="$(init_state)"
    [ "$st" = "exited 0" ] && [ "$(init_finished)" != "$init_fin" ] && break
    # `restart: "no"` (docker-compose.yml:531): a non-zero exit is terminal, so
    # stop waiting for the timeout and report it now.
    case "$st" in "exited "*) break ;; esac
    sleep 2; done
else
  for _ in $(seq 1 90); do
    st="$(init_state)"
    [ "$st" = "exited 0" ] && break
    case "$st" in "exited "*) break ;; esac
    sleep 2; done
fi
if [ "$st" != "exited 0" ]; then
  echo "install.sh: the Keycloak setup container did not finish OK ($st); check: docker logs my_keycloak_admin_init"
  # On the reset path the user ASKED for this repair (the fresh Keycloak has no
  # permanent admin until the one-shot exits 0), so a silent rc 0 would send them
  # to a broken login. A normal re-run keeps today's contract: warn, exit 0.
  if [ "$kc_reset" = 1 ]; then exit 1; fi
fi
./scripts/ce-doctor.sh >/dev/null 2>&1 || echo "install.sh: ce-doctor reported warnings; run ./scripts/ce-doctor.sh"
echo "Open: http://$host:3000 (Dashboard)  http://$host:3001 (Workflow Builder)  http://$host:3002 (Docs)  http://$host:8000/v1 (API)  http://$host:$kport (Keycloak)"
echo "Sign in as admin@core.local; its password is KEYCLOAK_REALM_ADMIN_PASSWORD in the .env file in this directory (read it with: grep '^KEYCLOAK_REALM_ADMIN_PASSWORD=' .env)"
[ "$mode" = lan ] && echo "LAN access is ON (listening on $bind). Undo with: ./scripts/install.sh --no-lan"
exit 0
