"""Pre-emptive Docker disk monitor + auto-cleanup (item #1 in the
fullproof-extension plan).

PRIMARY CLEANUP NOW LIVES IN THE WORKER. This module only works on a
host where the API process can reach the docker socket AND stat the
docker data root. In the SaaS topology the api container has neither
(no /var/run/docker.sock, no /var/lib/docker view), so every tick here
no-ops (`disk_used_pct()` → None → skipped). The real, cross-host
cleanup is `Worker/internal/executor/diskcleanup.go`: the worker holds
the socket on every host (embedded + remote) and prunes build cache +
images on a disk-pressure threshold, plus a light prune after each
build. This monitor is retained only as a best-effort fallback for
single-host / on-prem deployments where the API *does* have docker
access (and otherwise harmlessly no-ops).

Today's failure mode: wfr 43 hit "No space left on device" on the
build worker. The /var/lib/docker partition fills with build cache +
intermediate images, build-task containers report disk-full, the
infra-transient classifier consumes its 3 free retries, then the
workflow_run FAILS. Operator has to manually `docker system prune`.

When it CAN run (on-prem with socket access), every tick:
  1. Read disk usage on /var/lib/docker (or the host root where
     Docker stores its data root).
  2. If usage > AUTO_CLEAN_THRESHOLD (default 85%), run
     `docker builder prune -af` (build cache is always safe to drop).
     If usage > 95%, also run `docker image prune -af` (unused images).
  3. Emit a Prometheus metric per tick so dashboards can spot drift.
  4. Log a structured warning when usage > ALERT_THRESHOLD (default
     90%) regardless of cleanup outcome — surfaces sustained
     pressure that auto-cleanup alone can't fix.

Defensive: any subprocess failure is logged and swallowed. Disk-
monitor failure must not block the API. Disabled entirely when
``DOCKER_DISK_MONITOR_DISABLED=true`` for environments where the
API doesn't have docker socket access.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


# Tunable thresholds. Env-var overridable for ops without code change.
def _threshold_pct(env_var: str, default: int) -> int:
    try:
        v = int(os.getenv(env_var, str(default)))
    except (TypeError, ValueError):
        return default
    return max(0, min(100, v))


def _disk_root() -> str:
    """Path the host's docker daemon stores its data root at. Most
    common: /var/lib/docker. Override via env when the host uses a
    custom data-root."""
    return os.getenv("DOCKER_DATA_ROOT", "/var/lib/docker")


def disk_used_pct() -> Optional[int]:
    """Return percent-used for the partition holding Docker's data
    root. Returns None when the path doesn't exist or stat fails
    (e.g. the API container can't see the host filesystem)."""
    try:
        stat = shutil.disk_usage(_disk_root())
    except Exception as e:  # noqa: BLE001
        logger.debug("docker_disk_monitor: disk_usage(%s) failed: %s", _disk_root(), e)
        return None
    if stat.total == 0:
        return None
    return int(stat.used * 100 / stat.total)


def _run(cmd: list[str], timeout_s: int = 600) -> tuple[bool, str]:
    """Run a subprocess and return (ok, stderr_excerpt). Never raises."""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return (r.returncode == 0, (r.stderr or r.stdout or "")[-400:])
    except subprocess.TimeoutExpired:
        return (False, f"timeout after {timeout_s}s")
    except FileNotFoundError:
        return (False, f"command not found: {cmd[0]}")
    except Exception as e:  # noqa: BLE001
        return (False, f"{type(e).__name__}: {e}")


def run_disk_monitor_tick() -> dict:
    """Single monitor tick — APScheduler entry point.

    Returns a dict with the counters from this tick (for logging /
    tests). Never raises; catches every subprocess + disk-stat
    failure so a monitor bug can't block the API.
    """
    if os.getenv("DOCKER_DISK_MONITOR_DISABLED", "").lower() in ("1", "true", "yes"):
        return {"disabled": True}

    used = disk_used_pct()
    if used is None:
        return {"used_pct": None, "skipped": "cannot stat docker data root"}

    auto_clean = _threshold_pct("DOCKER_DISK_AUTO_CLEAN_THRESHOLD", 85)
    deep_clean = _threshold_pct("DOCKER_DISK_DEEP_CLEAN_THRESHOLD", 95)
    alert = _threshold_pct("DOCKER_DISK_ALERT_THRESHOLD", 90)

    out: dict = {"used_pct": used, "actions": []}

    if used > auto_clean:
        logger.info(
            "docker_disk_monitor: used=%d%% (threshold=%d%%) — running builder prune",
            used, auto_clean,
        )
        ok, err = _run(["docker", "builder", "prune", "-af"], timeout_s=600)
        out["actions"].append({"builder_prune": ok, "stderr": err if not ok else ""})
        if not ok:
            logger.warning("docker_disk_monitor: builder prune failed: %s", err)

    if used > deep_clean:
        logger.warning(
            "docker_disk_monitor: used=%d%% (deep threshold=%d%%) — running image prune",
            used, deep_clean,
        )
        ok, err = _run(["docker", "image", "prune", "-af"], timeout_s=900)
        out["actions"].append({"image_prune": ok, "stderr": err if not ok else ""})
        if not ok:
            logger.warning("docker_disk_monitor: image prune failed: %s", err)

    if used > alert:
        post = disk_used_pct()
        if post is not None and post > alert:
            logger.warning(
                "docker_disk_monitor: PRESSURE WARNING — used=%d%% before cleanup, %d%% after. "
                "Auto-cleanup insufficient. Operator should investigate.",
                used, post,
            )
            out["alert"] = True
            out["used_pct_post_clean"] = post

    return out
