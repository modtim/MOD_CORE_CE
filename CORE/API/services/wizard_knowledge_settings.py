"""services/wizard_knowledge_settings.py — wizard-knowledge-pack (openspec
`the internal design notes` Phase 1, task 1.4): the
platform-settings surface for the knowledge-pack verification pubkey + the
three mutable "apply target" keys packs can seed.

Pubkey decision (psr-vs-constant): `services/platform_settings.register()`
(checked at `CORE/API/services/platform_settings.py:155`) takes only
`key, type_, default, env_fallback, restart_required, validator,
description, group` — there is no `ui_editable`/`hidden`/admin-UI-exclusion
kwarg, and every registered key is listed through the generic admin
settings surface by group. Task 1.4 requires the verification pubkey to
NOT appear in the platform-admin settings UI, so per the brief's
resolution it is kept OUT of the psr registry entirely and is instead a
plain module constant, loaded from an install-time file (never an env
var — "no env vars for behaviour").

1524 (persistence fix): the 10-14-generated key's PUBLIC half (fingerprint
2d:43:fc:4b…ff:b8) now ships in the repo/image at
`CORE/API/config/wizard_knowledge_pack_pubkey.pem` (`SHIPPED_PUBKEY_FILE_PATH`),
since it is identical for every install and customers only ever verify
packs MOD itself signs -- it is public by design, like an apt signing key.
`wizard_knowledge_pack_pubkey()` reads, in order: the operator-override
file at `PUBKEY_FILE_PATH` (unchanged location/semantics, for an operator
who wants to rotate without a new image build), then the shipped default
file, then "". Shipping the default means a plain container recreate no
longer silently loses verification the way the old /etc/mod-only path did
-- that path had nothing mounted there, so `wizard_knowledge_pack_pubkey()`
returned "" after every recreate and no pack verified, failing closed
without anyone noticing.

The other three keys ARE mutable/admin-editable platform settings
(group="wizard", additive — no migration, no new tables per the Impact
section at proposal.md:58): pack-content "apply targets" that 986d wires
into the actual consumers (`node_builder/known_bases.py`, the Containerizer
prompt cheatsheet, `node_builder/failure_category.py`). This module only
registers + exposes typed readers; it does not consume them.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from services import platform_settings as psr  # leaf module: no audit cycle

logger = logging.getLogger(__name__)

GROUP = "wizard"

# ---------------------------------------------------------------------------
# Verification pubkey — module constant, NOT a psr setting (see docstring).
# ---------------------------------------------------------------------------

# Install-time file path (not an env var controlling behaviour — this is a
# fixed on-disk location an installer/operator may drop a key into; its
# absence is the normal/default state, in which case the shipped default
# below is used instead).
PUBKEY_FILE_PATH = "/etc/mod/wizard_knowledge_pack_pubkey.pem"

# The public key MOD ships in every install/image, baked into the repo at
# CORE/API/config/wizard_knowledge_pack_pubkey.pem (copied into the API
# image by the Dockerfile's `COPY . .`). Public by design -- identical for
# every install, like an apt signing key -- so shipping it is not a secret
# leak; only the matching PRIVATE key (Vault
# apps/mod_wizard_knowledge_pack_ed25519) must never appear here.
SHIPPED_PUBKEY_FILE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "wizard_knowledge_pack_pubkey.pem",
)


def wizard_knowledge_pack_pubkey() -> str:
    """The ed25519 public key (base64 raw-bytes text) used to verify signed
    knowledge packs, or "" when neither source is available (no pack
    verifies).

    Read order: the operator-override file at `PUBKEY_FILE_PATH` first
    (an operator who wants to rotate without a new image build), then the
    shipped default at `SHIPPED_PUBKEY_FILE_PATH` (baked into the image,
    survives container recreates), then "".

    Never a psr setting — task 1.4 requires this stays out of the
    platform-admin settings UI. Read fresh each call (no caching) since
    it only changes at install/rotation time, which is rare and already
    requires a file-system write by an operator.
    """
    for path in (PUBKEY_FILE_PATH, SHIPPED_PUBKEY_FILE_PATH):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read().strip()
            if content:
                return content
        except OSError:
            continue
    return ""


# ---------------------------------------------------------------------------
# Mutable apply-target settings (psr-registered, group="wizard")
# ---------------------------------------------------------------------------

DENYLIST_ADDITIONS_KEY = "wizard_knowledge_denylist_additions"
PROMPT_OVERRIDES_KEY = "wizard_knowledge_prompt_overrides"
FAILURE_HINTS_KEY = "wizard_knowledge_failure_hints"


def _validate_denylist_additions(value: Any) -> None:
    if not isinstance(value, list):
        raise ValueError(
            f"{DENYLIST_ADDITIONS_KEY} must be a list of strings, got "
            f"{type(value).__name__}")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"{DENYLIST_ADDITIONS_KEY} entries must be non-empty "
                f"strings: {item!r}")


def _validate_prompt_overrides(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError(
            f"{PROMPT_OVERRIDES_KEY} must be a dict, got "
            f"{type(value).__name__}")
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError(
                f"{PROMPT_OVERRIDES_KEY} entries must be str -> str: "
                f"{k!r}: {v!r}")


_REQUIRED_HINT_KEYS = {"stderr_regex", "category", "fix_hint"}


def _validate_failure_hints(value: Any) -> None:
    if not isinstance(value, list):
        raise ValueError(
            f"{FAILURE_HINTS_KEY} must be a list of objects, got "
            f"{type(value).__name__}")
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(
                f"{FAILURE_HINTS_KEY} entries must be objects (dicts): "
                f"{item!r}")
        missing = _REQUIRED_HINT_KEYS - item.keys()
        if missing:
            raise ValueError(
                f"{FAILURE_HINTS_KEY} entry missing keys {sorted(missing)}: "
                f"{item!r}")
        for req_key in _REQUIRED_HINT_KEYS:
            if not isinstance(item[req_key], str):
                raise ValueError(
                    f"{FAILURE_HINTS_KEY} entry {req_key!r} must be a "
                    f"string: {item[req_key]!r}")


psr.register(
    DENYLIST_ADDITIONS_KEY,
    list,
    [],
    validator=_validate_denylist_additions,
    group=GROUP,
    description="wizard-knowledge-pack: pack-appended hallucinated "
                "base-image refs, consumed by "
                "node_builder.known_bases.is_known_hallucinated as a "
                "secondary check alongside the hardcoded frozenset. "
                "Default [] = no pack-sourced additions.",
)
psr.register(
    PROMPT_OVERRIDES_KEY,
    dict,
    {},
    validator=_validate_prompt_overrides,
    group=GROUP,
    description="wizard-knowledge-pack: Containerizer prompt cheatsheet "
                "override map (str -> str). Default {} = no overrides.",
)
psr.register(
    FAILURE_HINTS_KEY,
    list,
    [],
    validator=_validate_failure_hints,
    group=GROUP,
    description="wizard-knowledge-pack: pack-sourced build-failure hints, "
                "mirroring node_builder.failure_category's shape — each "
                "entry is {stderr_regex, category, fix_hint}. Default "
                "[] = no pack-sourced hints.",
)

MAX_PACK_B64_BYTES_KEY = "wizard_knowledge_max_pack_b64_bytes"


def _validate_max_pack_b64_bytes(value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(
            f"{MAX_PACK_B64_BYTES_KEY} must be a positive int, got {value!r}")


psr.register(
    MAX_PACK_B64_BYTES_KEY,
    int,
    8 * 1024 * 1024,
    validator=_validate_max_pack_b64_bytes,
    group=GROUP,
    description="wizard-knowledge-pack: max length (chars) of the air-gap "
                "upload's `pack_bytes_b64` field accepted by "
                "`POST /wizard/update-knowledge`. A knowledge pack is a "
                "small JSON document (denylist/prompt-override/hint lists), "
                "not bulk media, so this is a request-size DoS guard, not a "
                "real content-size limit. Default 8 MiB of base64 text "
                "(~6 MiB decoded). Read via "
                "services.wizard_knowledge_settings.max_pack_b64_bytes.",
)


AUTO_UPDATE_ENABLED_KEY = "wizard_knowledge_auto_update_enabled"

psr.register(
    AUTO_UPDATE_ENABLED_KEY,
    bool,
    True,
    group=GROUP,
    description="wizard-knowledge-pack (KP.7): gates the scheduler's daily "
                "`_check_for_knowledge_pack_update` job "
                "(services/scheduler.py), which polls the distribution "
                "manifest and auto-applies a newer signed pack through the "
                "same verify_signature/download_pack/verify_content_hash/"
                "apply_pack pipeline POST /wizard/update-knowledge uses. "
                "Default True (auto-update on, matching the free/pro-ish "
                "default design.md describes) — an install that "
                "wants manual-only updates sets this False. A single "
                "platform-wide default is used here; design.md's per-tier "
                "default nuance is not implementable as one psr default "
                "and is not attempted.",
)


def auto_update_enabled(db: Session, tenant_id: Optional[int] = None) -> bool:
    try:
        return bool(psr.get_setting(AUTO_UPDATE_ENABLED_KEY, db=db,
                                    tenant_id=tenant_id))
    except Exception:  # noqa: BLE001 — fail closed to the registered default
        logger.warning("wizard_knowledge_settings: auto-update-enabled read "
                       "failed; returning True (registered default)")
        return True


def denylist_additions(db: Session,
                       tenant_id: Optional[int] = None) -> List[str]:
    try:
        return list(psr.get_setting(DENYLIST_ADDITIONS_KEY, db=db,
                                    tenant_id=tenant_id))
    except Exception:  # noqa: BLE001 — fail closed to the registered default
        logger.warning("wizard_knowledge_settings: denylist-additions read "
                       "failed; returning []")
        return []


def prompt_overrides(db: Session,
                     tenant_id: Optional[int] = None) -> Dict[str, str]:
    try:
        return dict(psr.get_setting(PROMPT_OVERRIDES_KEY, db=db,
                                    tenant_id=tenant_id))
    except Exception:  # noqa: BLE001
        logger.warning("wizard_knowledge_settings: prompt-overrides read "
                       "failed; returning {}")
        return {}


def failure_hints(db: Session,
                  tenant_id: Optional[int] = None) -> List[Dict[str, str]]:
    try:
        return list(psr.get_setting(FAILURE_HINTS_KEY, db=db,
                                    tenant_id=tenant_id))
    except Exception:  # noqa: BLE001
        logger.warning("wizard_knowledge_settings: failure-hints read "
                       "failed; returning []")
        return []


def max_pack_b64_bytes(db: Session,
                       tenant_id: Optional[int] = None) -> int:
    try:
        return int(psr.get_setting(MAX_PACK_B64_BYTES_KEY, db=db,
                                   tenant_id=tenant_id))
    except Exception:  # noqa: BLE001 — fail closed to the registered default
        logger.warning("wizard_knowledge_settings: max-pack-b64-bytes read "
                       "failed; returning default")
        return 8 * 1024 * 1024
