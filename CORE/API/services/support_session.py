"""FX.104 (owner decision Q90=A) — remote support session: settings +
guard ONLY.

The interactive remote-session FEATURE itself is Planned: (no session
broker, no agent, no endpoint in this task). What ships:

  * ``support.remote_session_enabled``    — bool, default False. The
    guard: when a gov/DoD profile (cmmc / fedramp_high_il5, see
    PROFILE_REQUIRED_SETTINGS) is active it is FORCED False, re-derived
    from the active profile list at the enforcement point so a drifted
    DB value can never re-enable it (same pattern as FX.102's
    share.public_links_enabled).
  * ``support.remote_session_max_hours``  — int, default 4. Maximum
    session duration in hours (validator bounds it to 1..24).

``effective_remote_session(db)`` is the single enforcement point a
future session surface must call; today it is exercised by tests.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

SETTING_ENABLED = "support.remote_session_enabled"
SETTING_MAX_HOURS = "support.remote_session_max_hours"

# Gov/DoD profiles whose PROFILE_REQUIRED_SETTINGS force the switch
# False. Kept here (not re-parsed from the profile map) so the read-side
# force is explicit and stable.
GOV_PROFILES = ("cmmc", "fedramp_high_il5")


def max_hours_validator(value) -> None:
    """support.remote_session_max_hours: integer hours, 1..24."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            "support.remote_session_max_hours must be an integer number "
            "of hours")
    if not 1 <= value <= 24:
        raise ValueError(
            "support.remote_session_max_hours must be between 1 and 24")


def profile_forces_off(db=None) -> bool:
    """True iff an active gov/DoD profile requires the switch False."""
    # FX.162.p5e — CE carve: no profiles module -> nothing is forced.
    from utils.paid_import import optional_paid
    cprof = optional_paid("services.compliance_profiles")
    if cprof is None:
        return False

    for profile in cprof.active_profiles(db):
        if profile not in GOV_PROFILES:
            continue
        requirement = (cprof.PROFILE_REQUIRED_SETTINGS.get(profile) or {}).get(
            SETTING_ENABLED)
        if requirement is not None and requirement.get("req") == ("eq", False):
            return True
    return False


def effective_remote_session(db) -> dict:
    """The enforcement read: (enabled, max_hours, forced_off,
    forced_by). A gov profile force wins over the stored value."""
    from services import platform_settings as psr

    stored = psr.get_setting(SETTING_ENABLED, db=db)
    forced_by = None
    forced_off = False
    try:
        # FX.162.p5e — CE carve: no profiles module -> no force.
        from utils.paid_import import optional_paid
        cprof = optional_paid("services.compliance_profiles")
        for profile in (cprof.active_profiles(db)
                        if cprof is not None else []):
            requirement = (cprof.PROFILE_REQUIRED_SETTINGS.get(profile)
                           or {}).get(SETTING_ENABLED)
            if (profile in GOV_PROFILES and requirement is not None
                    and requirement.get("req") == ("eq", False)):
                forced_off = True
                forced_by = profile
    except Exception:  # noqa: BLE001 — read-side guard must not raise
        logger.warning("support session: profile force lookup failed")

    enabled = bool(stored) and not forced_off
    max_hours = psr.get_setting(SETTING_MAX_HOURS, db=db)
    if forced_off and isinstance(max_hours, int) and max_hours > 4:
        # gov posture: even the cap is clamped to the 4h default
        max_hours = 4
    return {
        "enabled": enabled,
        "max_hours": int(max_hours) if max_hours is not None else 4,
        "forced_off": forced_off,
        "forced_by": forced_by,
    }
