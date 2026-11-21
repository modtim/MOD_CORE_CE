"""GY.C9.s3 — hardened-container-posture DECLARATIONS for the compose

services that `CORE/API/tools/check_hardened_image.py` audits (non-root,
read-only rootfs, dropped caps).

Declaration-based, exactly like the residency and at-rest-encryption
declarations: these settings record what an operator (or a Lead) found
when they ran `check_hardened_image.py` against a built image by hand —
this module never calls Docker, never inspects a running container, and
never reads `docker-compose.yml` itself. The API container must not need
Docker-socket access to self-report (that's the opposite of what GY.C9
is hardening against). When the compose-level cap_drop/read_only/non-root
wiring lands (a separate, larger Lead/HW follow-up), the operator flips
these settings and the C12 check `hardened_deploy.compose_posture`
reflects it unchanged.

One setting per tracked service: ``hardening.compose_posture.api``,
``hardening.compose_posture.worker`` — each an enum
``unknown|partial|hardened`` (default ``unknown``). "unknown" means
nobody has recorded a result yet; "partial" means some but not all of
non-root/read-only/cap_drop are satisfied; "hardened" means
`check_hardened_image.py` reported the service fully hardened.
"""
from __future__ import annotations

SERVICES = ("api", "worker", "api-background")
SETTING_KEY_PREFIX = "hardening.compose_posture."
STATES = ("unknown", "partial", "hardened")
DEFAULT_STATE = "unknown"


def _register_settings() -> None:
    """Declare this module's tunables (register() no-ops on re-
    registration, so this is idempotent)."""
    from services import platform_settings as psr

    for service in SERVICES:
        psr.register(
            SETTING_KEY_PREFIX + service, str, DEFAULT_STATE,
            group="hardening",
            description="GY.C9.s3 — hardened-deploy posture for the "
                        f"{service} compose service: 'unknown' (nobody "
                        "has recorded a result), 'partial' (some but "
                        "not all of non-root/read-only/cap_drop are "
                        "satisfied) or 'hardened' "
                        "(CORE/API/tools/check_hardened_image.py "
                        "reported it fully hardened). Declaration — set "
                        "by an operator/Lead after running that tool by "
                        "hand; this setting never performs a live "
                        "docker inspect.",
        )


_register_settings()


def hardening_posture_status(db) -> dict:
    """Pure helper for the C12 check: {service: state} for every tracked
    service.

    Reads the two declaration settings. A missing, empty, or invalid
    value (anything outside unknown|partial|hardened) counts as
    "unknown" — the check never trusts a declaration it cannot parse.
    """
    from services import platform_settings as psr

    status = {}
    for service in SERVICES:
        try:
            raw = psr.get_setting(SETTING_KEY_PREFIX + service, db=db)
        except Exception:  # unreadable setting = nothing declared
            raw = None
        state = str(raw or "").strip().lower()
        status[service] = state if state in STATES else DEFAULT_STATE
    return status
