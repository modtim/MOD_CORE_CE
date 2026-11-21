#!/usr/bin/env bash
# =============================================================================
# lib/ce-host-resources.sh — 2400CR1y: cgroup-aware host resources.
#
# Sourced (never executed) by scripts/first-run-secrets.sh, scripts/ce-resize.sh
# and scripts/ce-doctor.sh. Those scripts used to size the CE_* rails from
# /proc/meminfo + nproc alone, which on a container whose cgroup is tighter than
# the host (docker run -m 4g --cpus 2, Docker Desktop's VM, systemd's user
# slice) reports the HOST's 125 GB / 48 CPUs and picks `large` for a 4 GB box.
#
# detect_host_resources [meminfo_path] sets:
#   HOST_RAM_GB       min(MemTotal, cgroup memory limit) in whole GB
#   HOST_CPUS         min(nproc, ceil(cpu quota / period))
#   HOST_CGROUP_NOTE  one line, e.g.
#                     "sizing: cgroup limit 4 GB / 2 CPU (host 125 GB / 48)",
#                     empty when no cgroup limit beat the host numbers.
#
# Every probe is guarded with `|| true`: under the callers' `set -euo pipefail`
# a failing probe leaves its value empty instead of aborting the install.
# With no RAM probe at all HOST_RAM_GB=0, which the callers size as `small`
# (the documented default under 7 GB). tests/test_ce_host_resources.sh.
#
# RAM  = min(MemTotal, cgroup limit)
#   v2: $CE_CGROUP_ROOT/memory.max            ("max" = no limit)
#   v1: $CE_CGROUP_ROOT/memory/memory.limit_in_bytes (sentinels >= 2^60 ignored)
# CPU  = min(nproc, ceil(quota / period))
#   v2: $CE_CGROUP_ROOT/cpu.max               ("max <period>" = no limit)
#   v1: $CE_CGROUP_ROOT/cpu/cpu.cfs_quota_us / cpu.cfs_period_us (quota -1 = none)
#
# CE_CGROUP_ROOT is a test hook (where the cgroup tree is read from, default
# /sys/fs/cgroup), like the existing CE_MEMINFO_PATH meminfo hook. Never set
# either in production.
# =============================================================================

CE_CGROUP_ROOT="${CE_CGROUP_ROOT:-/sys/fs/cgroup}"

# _read_num <path> -> digits of that file on stdout, empty if unreadable/absent.
_read_num() {
  local raw=''
  [ -f "$1" ] || return 0
  raw="$(cat "$1" 2>/dev/null || true)"
  raw="$(printf '%s' "$raw" | tr -dc '0-9')"
  [ -n "$raw" ] || return 0
  printf '%s' "$raw"
}

detect_host_resources() {
  local meminfo="${1:-/proc/meminfo}"
  local root="$CE_CGROUP_ROOT"
  local mem_host='' mem_limit='' cpu_host='' cpu_limit=''
  local raw='' parts='' q='' p='' v=''

  # --- host RAM: MemTotal (Linux), else macOS sysctl -------------------------
  if [ -r "$meminfo" ]; then
    v="$(grep -m1 '^MemTotal:' "$meminfo" 2>/dev/null | tr -dc '0-9' || true)"
    if [ -n "$v" ]; then mem_host="$(( v * 1024 ))"; fi
  fi
  if [ -z "$mem_host" ] && command -v sysctl >/dev/null 2>&1; then
    v="$(sysctl -n hw.memsize 2>/dev/null | tr -dc '0-9' || true)"
    [ -n "$v" ] && mem_host="$v"
  fi

  # --- cgroup RAM limit: v2 memory.max, then v1 memory/limit_in_bytes -------
  if [ -f "$root/memory.max" ]; then
    raw="$(cat "$root/memory.max" 2>/dev/null || true)"
    case "$raw" in
      max*) : ;;
      *) mem_limit="$(printf '%s' "$raw" | tr -dc '0-9')" ;;
    esac
  fi
  if [ -z "$mem_limit" ] && [ -f "$root/memory/memory.limit_in_bytes" ]; then
    mem_limit="$(_read_num "$root/memory/memory.limit_in_bytes")"
    # cgroup v1 "unlimited" is a huge sentinel (often 2^63-1); ignore >= 2^60.
    if [ -n "$mem_limit" ] && [ "$mem_limit" -ge 1152921504606846976 ]; then
      mem_limit=''
    fi
  fi

  # --- host CPUs: nproc, else macOS sysctl ----------------------------------
  if command -v nproc >/dev/null 2>&1; then
    cpu_host="$(nproc 2>/dev/null | tr -dc '0-9' || true)"
  fi
  if [ -z "$cpu_host" ] && command -v sysctl >/dev/null 2>&1; then
    cpu_host="$(sysctl -n hw.ncpu 2>/dev/null | tr -dc '0-9' || true)"
  fi

  # --- cgroup CPU limit: v2 cpu.max, then v1 cfs_quota_us/cfs_period_us -----
  if [ -f "$root/cpu.max" ]; then
    raw="$(cat "$root/cpu.max" 2>/dev/null || true)"
    set -- $raw
    q="$1"; p="$2"
    case "$q" in max*) q='' ;; esac
    p="$(printf '%s' "$p" | tr -dc '0-9')"
    if [ -n "$q" ] && [ -n "$p" ] && [ "$p" != 0 ]; then
      cpu_limit="$(( (q + p - 1) / p ))"
    fi
  elif [ -f "$root/cpu/cpu.cfs_quota_us" ] && [ -f "$root/cpu/cpu.cfs_period_us" ]; then
    q="$(cat "$root/cpu/cpu.cfs_quota_us" 2>/dev/null || true)"
    p="$(cat "$root/cpu/cpu.cfs_period_us" 2>/dev/null || true)"
    case "$q" in -*) q='' ;; esac          # -1 = no limit
    q="$(printf '%s' "$q" | tr -dc '0-9')"
    p="$(printf '%s' "$p" | tr -dc '0-9')"
    if [ -n "$q" ] && [ -n "$p" ] && [ "$p" != 0 ]; then
      cpu_limit="$(( (q + p - 1) / p ))"
    fi
  fi

  # --- min() + the note when a cgroup limit beat the host -------------------
  # 2400CR1y fix: the effective (min) numbers are what the rails are sized from,
  # so HOST_RAM_GB must come from the CGROUP limit when it won — the first draft
  # only flagged mem_won and still divided MemTotal, which sized a 4 GB / 2-CPU
  # dind as `large` off the host's 125 GB.
  local mem_eff="$mem_host" cpu_eff="$cpu_host" mem_won=0 cpu_won=0
  if [ -n "$mem_limit" ] && { [ -z "$mem_host" ] || [ "$mem_limit" -lt "$mem_host" ]; }; then
    mem_eff="$mem_limit"
    mem_won=1
  fi
  if [ -n "$cpu_limit" ] && { [ -z "$cpu_host" ] || [ "$cpu_limit" -lt "$cpu_host" ]; }; then
    cpu_eff="$cpu_limit"
    cpu_won=1
  fi

  local host_cpus="$cpu_host"
  if [ -z "$host_cpus" ] || [ "$host_cpus" = 0 ]; then host_cpus=1; fi
  if [ -z "$cpu_eff" ] || [ "$cpu_eff" = 0 ]; then cpu_eff=1; fi
  HOST_RAM_GB="$(( mem_eff / 1073741824 ))"
  HOST_CPUS="$cpu_eff"

  HOST_CGROUP_NOTE=''
  if [ "$mem_won" = 1 ] || [ "$cpu_won" = 1 ]; then
    local eff_gb eff_cpu host_gb
    if [ "$mem_won" = 1 ]; then eff_gb="$(( mem_limit / 1073741824 ))"; else eff_gb="$HOST_RAM_GB"; fi
    eff_cpu="$HOST_CPUS"
    host_gb="$(( mem_host / 1073741824 ))"
    HOST_CGROUP_NOTE="sizing: cgroup limit ${eff_gb} GB / ${eff_cpu} CPU (host ${host_gb} GB / ${host_cpus})"
  fi
  return 0
}
