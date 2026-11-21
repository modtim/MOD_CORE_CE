"""GSEC Q21.2 — agent-version gate for worker least-privilege features.

The scoped/short-lived BYO-S3 storage creds issued by
``POST /workers/tasks/{id}/credentials`` are only consumable by the worker
release that ships the Q21.7 per-task creds refresher
(``CORE/Worker/internal/credsrefresher``): older releases fetch once and
hold the bundle for the container's whole life, so a short-lived session
would die mid-task. ``worker_supports_sts_refresh`` gates that path on the
worker's last-reported ``WorkerLicense.agent_version`` (heartbeat-updated
semver string); anything unknown, missing, or below the Q21 minimum stays
on the existing static-key bundle.
"""

from typing import Optional

# The held worker release carrying Q21 (sockproxy Q20 + creds refresher
# Q21.7). 1.0.76 (current CORE/Worker/VERSION) is ALREADY live on the fleet
# and does NOT contain the refresher, so the gate must be the NEXT release:
# 1.0.77.
Q21_MIN_AGENT_VERSION = "1.0.77"


def _parse_semver(version: Optional[str]):
    """Parse a semver-ish string into a tuple of ints, or None.

    Tolerates a leading ``v`` and extra segments (build metadata is
    dropped at the first non-numeric segment, after the major.minor.patch
    triple is read)."""
    if not version or not isinstance(version, str):
        return None
    v = version.strip()
    if v.startswith("v"):
        v = v[1:]
    parts = []
    for seg in v.split("."):
        if not seg.isdigit():
            break
        parts.append(int(seg))
    while len(parts) < 3:
        parts.append(0)
    if not parts:
        return None
    return tuple(parts[:3])


def worker_supports_sts_refresh(
    agent_version: Optional[str],
    minimum: str = Q21_MIN_AGENT_VERSION,
) -> bool:
    """True iff the worker reports an agent version >= ``minimum``.

    Unknown/None/malformed versions fail CLOSED (False) so a worker that
    never reported a version — or one with garbage — keeps today's
    static-key bundle and is never handed a session it cannot refresh.
    """
    got = _parse_semver(agent_version)
    floor = _parse_semver(minimum)
    if got is None or floor is None:
        return False
    return got >= floor
