"""services/knowledge_pack_sanitizer.py -- KP.4 (wizard-knowledge-pack).

Pure, I/O-free pre-signing redaction/validation pass for candidate recipes,
implementing the 5 steps of `the internal design notes`
lines 92-96 ("Why a Sanitization Step Before Publishing"):

    1. tenant-slug strings            -> reject
    2. email / private-IP / private-hostname strings -> reject
    3. node-name / workflow-name strings not in MOD's public-node list -> reject
    4. source-code-shaped strings (Python / Bash / JS) -> reject
    5. remaining content must be purely library names + base image refs +
       apt package lists + version strings -> anything else rejects

A candidate is rejected OUTRIGHT, never partially redacted-and-shipped
(design.md: "Rejected candidates are logged for human review ... they never
enter a pack").

This module never queries the tenant directory or the node catalogue: the
caller (e.g. KP.3's publisher) injects `tenant_slugs` and
`public_node_names` as plain sets, so the pass stays pure and pluggable.
"""
from __future__ import annotations

import re
from typing import Any, Iterator

# --- patterns ---------------------------------------------------------------

# Quantifiers bounded to RFC-ish max field lengths (local-part<=64,
# domain<=253, TLD<=24) so a long run of local-part-shaped chars with no
# '@' can't force O(n^2) backtracking on large inputs.
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}"
)
# RFC1918: 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 (+ loopback and
# 169.254/16 link-local); false positives are acceptable per the brief.
_PRIVATE_IP_RE = re.compile(
    r"\b(?:10(?:\.\d{1,3}){3}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}"
    r"|192\.168(?:\.\d{1,3}){2}|127(?:\.\d{1,3}){3}"
    r"|169\.254(?:\.\d{1,3}){2})\b"
)
# IPv6 ULA (fc00::/7, i.e. fc.. / fd..) and link-local (fe80::/10). A single
# bounded repetition (max 39 chars, the longest valid IPv6 literal) keeps
# this linear-time -- no adjacent unbounded quantifiers over the same class.
_PRIVATE_IPV6_RE = re.compile(
    r"\b(?:f[cd][0-9a-fA-F]{0,2}|fe[89ab][0-9a-fA-F]?):[0-9a-fA-F:]{1,39}\b",
    re.I,
)
# bounded to 253 chars (max DNS name length) to avoid O(n^2) backtracking.
_PRIVATE_HOST_RE = re.compile(r"\b[a-z0-9][a-z0-9.-]{0,251}\.(?:local|internal|lan)\b", re.I)

_SOURCE_MARKERS = ("def ", "function ", "#!/", "import ", "<?php", "require ", "=> ")
# upper bound (a "statement" longer than this isn't plausible for this
# heuristic) caps the backtracking cost when a run has no trailing ';'.
# recipe-shaped vocab (step 5)
_LIB_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.+-]*$")
_VERSION_RE = re.compile(r"^v?\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.\-]+)?$")
_IMAGE_REF_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9._-]+)?$")
_RECIPE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_NAME_FIELD_KEYS = {"node_name", "node", "workflow_name", "workflow", "node_key",
                    "workflow_key", "node_id", "workflow_id"}
# bounded per-segment lengths (identifier-shaped, not arbitrary) to avoid
# O(n^2) backtracking when a long run has no '-'/'_' segment boundary.
_NAME_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}(?:[-_][A-Za-z0-9]{1,63}){1,8}")


# --- the 5 checks (each returns a reason string, or None) -----------------

def _contains_tenant_slug(text: str, tenant_slugs: set[str]) -> str | None:
    if not tenant_slugs:
        return None
    tokens = re.split(r"[^A-Za-z0-9._-]+", text)
    for token in tokens:
        if token and (token in tenant_slugs or token.lower() in tenant_slugs):
            return f"contains customer-tenant slug {token!r}"
    return None


def _contains_email_or_private_net(text: str) -> str | None:
    m = _EMAIL_RE.search(text)
    if m:
        return f"contains email address {m.group(0)!r}"
    m = _PRIVATE_IP_RE.search(text)
    if m:
        return f"contains private IP {m.group(0)!r}"
    m = _PRIVATE_IPV6_RE.search(text)
    if m:
        return f"contains private IPv6 {m.group(0)!r}"
    m = _PRIVATE_HOST_RE.search(text)
    if m:
        return f"contains private hostname {m.group(0)!r}"
    return None


def _contains_unlisted_node_name(
    text: str, public_node_names: set[str], key: str | None = None
) -> str | None:
    if not public_node_names:
        return None
    lowered_public = {n.lower() for n in public_node_names}
    if key in _NAME_FIELD_KEYS:
        if text.strip().lower() not in lowered_public:
            return f"node/workflow name {text!r} is not in MOD's public-node list"
    # cheap O(n) prefilter: the token regex can never match without a
    # '-'/'_' segment separator -- skip the (bounded but still costly on
    # a long separator-free run) regex scan entirely when it can't match.
    if "-" not in text and "_" not in text:
        return None
    for token in _NAME_TOKEN_RE.findall(text):
        if token.lower() in lowered_public:
            continue
        if "node" in token.lower() or "workflow" in token.lower() or "flow" in token.lower():
            return f"node/workflow-name-shaped string {token!r} is not in MOD's public-node list"
    return None


def _looks_like_source_code(text: str) -> str | None:
    lines = text.splitlines()
    for line in lines:
        stripped = line.strip()
        if any(marker in stripped for marker in _SOURCE_MARKERS):
            return f"source-code-shaped line {stripped[:60]!r}"
    # O(n) str.split-based count (no regex backtracking risk at all): each
    # ';' before which there are >=2 non-newline chars since the previous
    # ';'/newline counts as one statement-looking segment.
    if text.count(";") >= 2:
        statement_count = 0
        for part in text.split(";")[:-1]:
            segment = part.rsplit("\n", 1)[-1]
            if len(segment) >= 2:
                statement_count += 1
                if statement_count > 1:
                    return "source-code-shaped string with multiple statement-looking lines"
    return None


def _is_recipe_shaped_content(text: str, allow_package_list: bool = False) -> str | None:
    body = text.strip()
    if not body:
        return None
    if _IMAGE_REF_RE.match(body) or _VERSION_RE.match(body) or _LIB_TOKEN_RE.match(body):
        return None
    # apt package list: only a leaf that came from a list value may hold
    # several package names; a scalar string must be one token.
    if allow_package_list:
        pieces = [p for p in re.split(r"[,;|\s]+", body) if p]
        if pieces and all(_LIB_TOKEN_RE.match(p) or _VERSION_RE.match(p) for p in pieces):
            return None
    return f"content {body[:60]!r} is not purely library names / base image refs / apt packages / versions"


# --- traversal ------------------------------------------------------------

def _string_leaves(
    value: Any, path: str = "recipe", key: str | None = None, in_list: bool = False
) -> Iterator[tuple[str, str | None, str, bool]]:
    if isinstance(value, str):
        yield path, key, value, in_list
    elif isinstance(value, dict):
        for k, v in value.items():
            kname = str(k)
            if not _RECIPE_KEY_RE.match(kname):
                yield path, key, kname, False  # non-recipe-shaped key trips step 5
            yield from _string_leaves(v, f"{path}.{kname}", kname)
    elif isinstance(value, (list, tuple, set)):
        for i, v in enumerate(value):
            yield from _string_leaves(v, f"{path}[{i}]", key, True)


def sanitize_candidate(
    recipe: dict,
    *,
    tenant_slugs: set[str] = frozenset(),
    public_node_names: set[str] = frozenset(),
) -> tuple[bool, list[str]]:
    """Returns (ok, reasons). ok=False means reject the candidate outright."""
    if not isinstance(recipe, dict):
        return False, ["recipe is not a dict"]
    reasons: list[str] = []
    try:
        leaves = list(_string_leaves(recipe))
    except Exception as exc:  # fail CLOSED: any traversal error rejects
        return False, [f"recipe: sanitizer error during traversal: {exc!r}"]
    for path, key, text, in_list in leaves:
        for label, check in (
            ("tenant slug check", lambda: _contains_tenant_slug(text, tenant_slugs)),
            ("email/private-net check", lambda: _contains_email_or_private_net(text)),
            ("node/workflow name check",
             lambda: _contains_unlisted_node_name(text, public_node_names, key)),
            ("source-code check", lambda: _looks_like_source_code(text)),
            ("recipe-shape check",
             lambda: _is_recipe_shaped_content(text, allow_package_list=in_list)),
        ):
            try:
                reason = check()
            except Exception as exc:  # fail CLOSED: an exception = reject
                reasons.append(f"{path}: {label} raised {exc!r} (fail-closed reject)")
                continue
            if reason:
                reasons.append(f"{path}: {reason}")
    return (not reasons), reasons
