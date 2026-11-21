"""Deploy image_override policy helper.

c3-J4b1 — 30.6.3 S11 security: tenant registry-allow + repo-pinned override check.
Design decomp/gx-c/c3-design.md :43 / :64.
"""

import re
import typing

from utils import http_errors

_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_HEX = re.compile(r"^[a-f0-9]{64}$")
_WS = re.compile(r"[\s\x00-\x1f]")

# c3-J4 TOKMERGE — ONE owner for the registry-allow setting name so the merged
# mint path (routers/api_keys.py, J2) and the deploy path cannot drift. Do NOT
# rename the setting: existing tenant rows are keyed on it
# (services/platform_settings.py:2523 registers it).
REGISTRY_ALLOW_SETTING = "stacks.deploy_token_registry_allow"


def parse_image_ref(ref: str) -> tuple[str, str, str | None, str | None]:
    """Normalise a Docker reference into (registry, repository, tag, digest).

    No registry component → docker.io. Single-component docker.io repo →
    library/<name>. No tag and no digest → tag latest. Reject on invalid input.
    """
    if not ref or len(ref) > 255 or _WS.search(ref) or ".." in ref:
        raise ValueError("invalid image reference")

    digest: str | None = None
    if "@" in ref:
        ref, raw = ref.split("@", 1)
        if raw.startswith("sha256:") or raw.startswith("SHA256:"):
            hex_only = raw[7:]
        elif raw.startswith("sha1:") or raw.startswith("SHA1:"):
            hex_only = raw[5:]
        else:
            hex_only = raw
        if not _HEX.match(hex_only):
            raise ValueError("invalid image reference")
        digest = f"sha256:{hex_only}"

    # Reject empty path components
    if any(p == "" for p in ref.split("/")):
        raise ValueError("invalid image reference")

    last_slash = ref.rfind("/")

    if last_slash < 0:
        # No "/" — single component image name
        colon = ref.rfind(":")
        if colon >= 0:
            solo_tag = ref[colon + 1:]
            if not _TAG_RE.match(solo_tag):
                raise ValueError("invalid image reference")
            return "docker.io", f"library/{ref[:colon]}", solo_tag, digest
        if digest is not None:
            return "docker.io", f"library/{ref}", None, digest
        return "docker.io", f"library/{ref}", "latest", None

    # Has "/" — registry + path + optional :tag
    ref_after_slash = ref[last_slash + 1:]
    if digest is not None:
        ref_after_slash = ref_after_slash.split("@")[0]

    colon_in_path = ref_after_slash.rfind(":")

    if digest is not None:
        tag = None
        name_part = ref_after_slash
    elif colon_in_path >= 0:
        tag = ref_after_slash[colon_in_path + 1:]
        if not _TAG_RE.match(tag):
            raise ValueError("invalid image reference")
        name_part = ref_after_slash[:colon_in_path]
    else:
        tag = "latest"
        name_part = ref_after_slash

    # Parse registry + repo from before the last "/"
    ref_before_slash = ref[:last_slash]
    first_slash = ref_before_slash.find("/")
    if first_slash < 0:
        registry = ref_before_slash
        repo = ""
    else:
        registry = ref_before_slash[:first_slash]
        repo = ref_before_slash[first_slash + 1:]

    # Non-registry component (no dot, no colon, not localhost) → docker.io/library
    if "." not in registry and ":" not in registry and registry != "localhost":
        registry = "docker.io"
        if repo:
            repo = f"library/{repo}"
        else:
            repo = f"library/{name_part}"
            name_part = ""

    full_repo = f"{repo}/{name_part}" if repo and name_part else (repo if repo else name_part)

    # Reject uppercase in repo path
    if full_repo and any(c.isupper() for c in full_repo):
        raise ValueError("invalid image reference")

    return registry, full_repo, tag, digest


def _normalise_allow_entry(entry: str) -> str | None:
    """Lowercase registry, strip trailing slash; None on bad input.

    Registry-only entries (e.g. "ghcr.io") match any repo.
    """
    if not isinstance(entry, str) or not entry.strip():
        return None
    entry = entry.strip()
    while entry.endswith("/"):
        entry = entry[:-1]
    if not entry:
        return None
    slash = entry.find("/")
    if slash < 0:
        return entry.lower()
    registry = entry[:slash].lower()
    repo = entry[slash + 1:].rstrip("/")
    return f"{registry}/{repo}" if repo else registry


def load_registry_allow(db) -> list[str]:
    """The tenant registry-allow list for the `image_override` policy.

    Fail closed: an unreadable/missing setting (None) or a non-list value
    yields [] — and `check_override` with an empty allow list denies every
    override. The list is returned unchanged otherwise (no re-ordering, no
    filtering — `_normalise_allow_entry` handles per-entry normalisation).
    Lazy import of platform_settings keeps this module import-light and
    avoids any import cycle.
    """
    from services import platform_settings as _ps
    value = _ps.get_setting(REGISTRY_ALLOW_SETTING, db=db)
    if not isinstance(value, list):
        return []
    return value


def check_override(
    service: str, current_ref: str, new_ref: str, allow: list[str]
) -> None:
    """Check a single override against the allow list. Raises 403 on violation."""
    try:
        c_reg, c_repo, _, _ = parse_image_ref(current_ref)
    except ValueError:
        raise http_errors.ApiError(403, "image_override_denied",
                                    "invalid image reference")

    try:
        n_reg, n_repo, _, _ = parse_image_ref(new_ref)
    except ValueError:
        raise http_errors.ApiError(403, "image_override_denied",
                                    "invalid image reference")

    if not allow:
        raise http_errors.ApiError(403, "image_override_denied",
                                    REGISTRY_ALLOW_SETTING)

    # Repo-pinned: registry+repo must match exactly
    if c_reg != n_reg:
        raise http_errors.ApiError(403, "image_override_denied",
                                    "registry changed")
    if c_repo != n_repo:
        raise http_errors.ApiError(403, "image_override_denied",
                                    "repository changed")

    # Allow-prefix boundary match
    ok = False
    for ent in allow:
        e = _normalise_allow_entry(ent)
        if e is None:
            continue
        slash = e.find("/")
        if slash < 0:
            # Entry is bare registry — matches any repo under it
            if n_reg == e:
                ok = True
                break
            continue
        e_reg = e[:slash]
        e_repo = e[slash + 1:]
        if n_reg == e_reg and (n_repo == e_repo or n_repo.startswith(e_repo + "/")):
            ok = True
            break
    if not ok:
        raise http_errors.ApiError(403, "image_override_denied",
                                    "outside " + REGISTRY_ALLOW_SETTING)


def check_overrides(
    overrides: dict, current_refs: dict, allow: list[str]
) -> None:
    """Iterate overrides dict, check each against current_refs."""
    for svc, new_ref in overrides.items():
        if svc not in current_refs:
            raise http_errors.ApiError(403, "image_override_denied",
                                        "unknown service")
        check_override(svc, current_refs[svc], new_ref, allow)
