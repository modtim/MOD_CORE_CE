#!/usr/bin/env bash
# Install the AppArmor profile the rootless BuildKit sidecar needs (node builder).
# Run once per host with root:  sudo ./scripts/install-buildkit-apparmor.sh
# Then:  docker compose --profile builder up -d
#
# Only needed where the kernel gates unprivileged user namespaces through
# AppArmor (Ubuntu 23.10+/24.04/26.04). On other hosts it prints which
# CE_BUILDKIT_APPARMOR value to use instead.
set -euo pipefail
# This script takes no arguments. Reject any argument before a root check or
# any file write, so `sudo install-buildkit-apparmor.sh --help` never installs.
if [ "$#" -eq 1 ] && { [ "$1" = -h ] || [ "$1" = --help ]; }; then
  echo "usage: sudo $0   (takes no arguments; installs the mod-buildkitd AppArmor profile)"
  exit 0
fi
[ "$#" -eq 0 ] || { echo "usage: sudo $0   (takes no arguments; exit 0=ok, exit 2=bad args)" >&2; exit 2; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$HERE/CORE/DockerContainers/buildkitd/apparmor-mod-buildkitd"
RESTRICT=/proc/sys/kernel/apparmor_restrict_unprivileged_userns

if [ ! -d /sys/module/apparmor ] || ! command -v apparmor_parser >/dev/null 2>&1; then
  echo "AppArmor is not active, or apparmor_parser is not installed, on this host. Put this in .env:  CE_BUILDKIT_APPARMOR=unconfined"
  exit 0
fi
if [ ! -e "$RESTRICT" ]; then
  echo "This kernel does not gate user namespaces through AppArmor."
  echo "The stock Docker profile still blocks the mounts BuildKit needs, so put this in .env:  CE_BUILDKIT_APPARMOR=unconfined"
  exit 0
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "run as root (sudo $0)" >&2
  exit 1
fi
install -m 0644 "$SRC" /etc/apparmor.d/mod-buildkitd
apparmor_parser -r /etc/apparmor.d/mod-buildkitd
echo "loaded AppArmor profile mod-buildkitd (persisted in /etc/apparmor.d, reloaded at boot)."
echo "Now:  docker compose --profile builder up -d"
