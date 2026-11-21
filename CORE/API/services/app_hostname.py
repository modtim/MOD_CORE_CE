"""GX.A2 — app hostname: reserved-label registry, slug grammar, hostname compute.

An app is reachable at ``<slug>.<BASE_DOMAIN>``. The slug is a single
DNS label: ``^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?`` (63 chars max, no dots —
a dot in the slug would let it escape the zone, §7.1). The FQDN is always
COMPUTED from the tenant-scoped slug + the deployment's BASE_DOMAIN; user
input never flows into the Caddyfile past the validated slug.

The reserved-label registry is a code constant (not DB-backed) so it cannot
drift per-tenant: a slug may never shadow a platform service hostname.
"""
from __future__ import annotations

import os
import re

from fastapi import HTTPException, status

# Single-label grammar: starts/ends alphanumeric, dashes inside, 63 max.
SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

# §1.1 reserved subdomain registry (hard block at create + rename).
RESERVED_LABELS = frozenset({
    "api", "dashboard", "workflows", "s3", "models", "docs", "mcp",
    "status", "grafana", "tenant-harbor", "mod-headscale", "auth",
    "apps", "harbor",
})


def base_domain() -> str:
    return os.getenv("BASE_DOMAIN", "localhost").strip().lower().rstrip(".")


def keycloak_label() -> str | None:
    """The first label of the live KEYCLOAK_HOSTNAME, when it is a
    subdomain of BASE_DOMAIN — that label is reserved too (it is the
    realm's own host, not an app host). None when Keycloak sits on the
    bare BASE_DOMAIN (unrouteable as a slug anyway)."""
    kc = os.getenv("KEYCLOAK_HOSTNAME", "").strip().lower().rstrip(".")
    base = base_domain()
    if not kc or kc == base:
        return None
    if kc.endswith("." + base):
        label = kc[: -(len(base) + 1)]
        if SLUG_RE.match(label):
            return label
    return None


def is_reserved_slug(slug: str) -> bool:
    label = slug.strip().lower()
    if label in RESERVED_LABELS:
        return True
    kc = keycloak_label()
    return kc is not None and label == kc


def validate_slug(slug: str) -> None:
    """Raise HTTPException(400) unless ``slug`` is a legal, unreserved label.

    Returns the slug lowercased when valid (callers rely on that).
    """
    if not isinstance(slug, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="slug must be a string",
        )
    label = slug.strip().lower()
    if not label:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="slug is required",
        )
    if not SLUG_RE.match(label):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "slug must match ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ "
                "(lowercase letters, digits, single dashes; 63 chars max; "
                "no dots)"
            ),
        )
    if is_reserved_slug(label):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"slug {label!r} is reserved for platform services",
        )
    return label


def normalize_slug(slug: str) -> str:
    """Validate and return the canonical (lowercased) slug."""
    return validate_slug(slug)


def compute_hostname(slug: str) -> str:
    """FQDN for a validated slug: ``<slug>.<BASE_DOMAIN>``.

    Raises 400 if the slug is invalid — the hostname is always derived
    from a grammar-checked label, never from free text.
    """
    label = normalize_slug(slug)
    return f"{label}.{base_domain()}"
