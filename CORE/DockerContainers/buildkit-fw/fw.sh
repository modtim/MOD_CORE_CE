#!/bin/sh
# Egress policy for the BuildKit sidecar netns: internet yes, private/LAN no.
#   BUILDKIT_FW_ALLOW  space-separated CIDRs allowed BEFORE the deny list
#                      (e.g. the optional local registry:2 container as a /32). Default: none.
#   BUILDKIT_FW_MODE   enforce (default) | off
# Fails closed: if the rules cannot be installed the container exits non-zero,
# and buildkitd (depends_on: service_healthy) never starts.
set -eu
MODE="${BUILDKIT_FW_MODE:-enforce}"
ALLOW="${BUILDKIT_FW_ALLOW:-}"
CHAIN=MOD_BUILD_OUT

DENY4="0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.168.0.0/16 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4"
DENY6="fc00::/7 fe80::/10 ff00::/8"

apply() {
  ipt="$1"; deny="$2"
  "$ipt" -N "$CHAIN" 2>/dev/null || "$ipt" -F "$CHAIN"
  "$ipt" -A "$CHAIN" -o lo -j ACCEPT
  "$ipt" -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  for c in $ALLOW; do
    case "$c" in *:*) [ "$ipt" = ip6tables ] || continue ;; *) [ "$ipt" = iptables ] || continue ;; esac
    "$ipt" -A "$CHAIN" -d "$c" -j ACCEPT
  done
  for c in $deny; do
    "$ipt" -A "$CHAIN" -d "$c" -j REJECT --reject-with "$( [ "$ipt" = iptables ] && echo icmp-admin-prohibited || echo icmp6-adm-prohibited )"
  done
  "$ipt" -A "$CHAIN" -j ACCEPT
  "$ipt" -C OUTPUT -j "$CHAIN" 2>/dev/null || "$ipt" -I OUTPUT 1 -j "$CHAIN"
}

if [ "$MODE" = enforce ]; then
  apply iptables "$DENY4"
  # IPv6 is off on the default compose network; apply only if the netns has it.
  if [ -e /proc/net/if_inet6 ] && [ -s /proc/net/if_inet6 ]; then apply ip6tables "$DENY6"; fi
  echo "buildkit-fw: egress policy enforced (allow='${ALLOW}')"
else
  echo "buildkit-fw: BUILDKIT_FW_MODE=$MODE - NO egress policy (builds can reach the LAN)" >&2
fi
touch /tmp/fw-ready
exec sleep infinity
