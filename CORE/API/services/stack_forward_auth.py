"""GX.S1c-stacks — forward-auth on PERSISTENT STACK ingress routes.

The stack Caddy sites are rendered by the EXTERNAL caddy-stack-ingress
container (this API only serves its on-demand-TLS `ask` seam —
routers/persistent_stacks/_shared.py `_ingress_authorized_domain`), so the
wiring lands in three seams instead of a rendered site:

  1. This module: host → (stack, auth cfg) resolution from the stack's
     stored compose_spec `x-mod.ingress.auth` block
     {require_login, allowed_groups, public_paths}, plus the Caddy snippet
     renderer that reuses `app_forward_auth.forward_auth_directives` +
     `auth_flow_directives` (one canonical forward-auth rendering).
  2. routers/persistent_stacks `GET /persistent-stacks/ingress/auth-snippet`
     (internal, guarded by the same optional ingress token as the `ask`
     seam): caddy-stack-ingress fetches the snippet per stack host and drops
     it verbatim into the on-demand site body, BEFORE its own reverse_proxy
     directive (forward_auth must evaluate before the handler).
  3. routers/apps `GET /apps/auth/verify`: when the inbound Host is not an
     App, it falls back to `get_stack_by_hostname` and enforces the same
     require_login / tenant / allowed_groups checks. A Bearer token is
     validated against the platform JWKS (audience fastapi-app — every
     platform client carries it) and pinned to the stack's tenant
     (or accepted from a platform admin); there is no per-stack KC client.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

import models

import secrets

from services import app_forward_auth
from keycloak_auth import KEYCLOAK_AUDIENCE

from product_names import themed_logger
logger = themed_logger(__name__, 'stacks')

# The ingress-side allowlist statuses — same set the on-demand-TLS `ask`
# seam uses (_shared._ACTIVE_STATUSES), so a host the ingress serves a cert
# for is exactly a host this resolver may authorise forward-auth for.
_STACK_ACTIVE_STATUSES = ("running", "deploying", "migrating")


def _stack_in_good_standing(stack: models.PersistentStack) -> bool:
    """Intended-up (`desired_state == 'running'`, so a scaled-to-zero stack is
    refused) and active (not torn down / failed)."""
    return (getattr(stack, "desired_state", None) == "running"
            and stack.status in _STACK_ACTIVE_STATUSES)


def _stack_default() -> dict:
    """The stack-seam default config: the App defaults MINUS `fail_closed`.

    c2-design.md:84 — the stack-ingress seam keeps its platform-only posture
    (`security.stack_tokens_fail_closed`); a per-stack `fail_closed` is out of
    scope, so it must never appear in a stack GET/PUT body, stored block or
    audit old/new (showing `fail_closed: true` would wrongly tell an admin the
    stack is fail-closed when it is not).
    """
    return {k: v for k, v in app_forward_auth.default_config().items() if k != "fail_closed"}


def stack_auth_config(stack: models.PersistentStack) -> dict:
    """The stack's forward-auth config from its stored compose spec's top-level
    `x-mod.ingress.auth` block, with defaults applied.

    compose_spec is a JSONB column that SQLAlchemy hands back as a dict
    (never call .astext on it — custom JSONB throws). Missing / non-dict
    blocks mean "public stack" (require_login False, no forward_auth).
    """
    spec = getattr(stack, "compose_spec", None)
    if not isinstance(spec, dict):
        return _stack_default()
    xmod = spec.get("x-mod")
    if not isinstance(xmod, dict):
        return _stack_default()
    ingress = xmod.get("ingress")
    if not isinstance(ingress, dict):
        return _stack_default()
    auth = ingress.get("auth")
    if not isinstance(auth, dict):
        return _stack_default()
    # Expand allowed_groups through the group-name alias map so a token
    # carrying the legacy name (minio_tenant_admin) matches a config that
    # references the canonical new name (s3_tenant_admin) and vice versa.
    raw = auth.get("allowed_groups") or []
    expanded = set()
    for g in raw:
        from utils.keycloak_client import _expand_group_name
        expanded.update(_expand_group_name(str(g)))
    return {
        "require_login": bool(auth.get("require_login", False)),
        "allowed_groups": sorted(expanded),
        "public_paths": [str(p) for p in (auth.get("public_paths") or [])],
    }


def get_stack_by_hostname(db, host: str) -> Optional[Tuple[models.PersistentStack, dict]]:
    """The live, exposed, good-standing stack serving this hostname, paired
    with its forward-auth config — the stack sibling of
    `app_forward_auth.get_app_by_hostname` (the verify endpoint falls back to
    this when no App owns the Host).

    Host sources mirror the on-demand-TLS `ask` allowlist (source 1 — the
    public-exposure trio; the legacy `x-mod.tls` fqdn path is cert-only and
    carries no `x-mod.ingress.auth` config, so it does not authorise
    forward-auth): public_hostname, custom_domain, friendly_fqdn. Port and
    case are ignored. Returns (None, None) → caller treats as unknown host.
    """
    host = (host or "").split(":")[0].strip().lower()
    if not host:
        return None, None
    try:
        from crud import persistent_stacks_shared as _pss_crud
        rows = _pss_crud.get_exposed_stacks(db)
    except Exception as exc:  # fails closed like the ask seam
        logger.warning("stack host resolution failed: %s", exc)
        return None, None
    for stack in rows:
        if not getattr(stack, "exposed", False):
            continue
        if not _stack_in_good_standing(stack):
            continue
        names = {str(getattr(stack, "public_hostname", None) or "").strip().lower(),
                 str(getattr(stack, "custom_domain", None) or "").strip().lower(),
                 str(getattr(stack, "friendly_fqdn", None) or "").strip().lower()}
        if host in names:
            return stack, stack_auth_config(stack)
    return None, None


def stack_forward_auth_site(stack: models.PersistentStack) -> str:
    """The Caddy body snippet for one stack site: a complete ``route { }``
    block containing the PUBLIC MOD-handled OIDC flow directive
    (``/_mod/auth*`` → API, rendered FIRST so it intercepts before
    forward_auth) and the ``forward_auth`` directive — or "" when the stack
    does not opt in (require_login false / no auth block).

    The ingress drops this snippet VERBATIM inside the site block, before
    its ``reverse_proxy`` handler.  The ``route`` wrapper is critical: Caddy
    sorts directives by its internal priority at the site-block level, NOT
    by literal position, so without the ``route`` container
    ``forward_auth`` would run before ``handle_path`` — infinite login loop
    (same BUG.B78 root cause as ``render_app_site``).  Literal ordering is
    preserved only inside a ``route { }`` block.

    Inside the ``route { }`` the caller must add its own ``reverse_proxy``
    AFTER the route block to proxy authenticated requests.
    """
    cfg = stack_auth_config(stack)
    if not cfg["require_login"]:
        return ""

    from services import app_forward_auth

    # auth_flow_directives / forward_auth_directives output uses \t at line
    # start.  We need \t\t inside the route { } block (one level deeper for
    # the route container's position in the site body).
    def _reindent(s: str) -> str:
        out: list[str] = []
        for line in s.split("\n"):
            if not line:
                continue
            if line.startswith("\t"):
                out.append("\t\t" + line[1:])
            else:
                out.append("\t\t" + line)
        return "\n".join(out) + "\n"

    auth_flow = app_forward_auth.auth_flow_directives()
    fa = app_forward_auth.forward_auth_directives(cfg)

    route_block = _reindent(auth_flow) + _reindent(fa)
    return f"route {{\n{route_block}}}\n"


# ---------------------------------------------------------------------------
# GX.S1c-stacks — browser login (MOD-handled OIDC code flow) on a stack host
#
# A stack has no KC client of its own, so the flow reuses the PLATFORM client
# (fastapi-app — the same audience every platform Bearer carries) and pins the
# redirect URI to the stack's own serving host. The session cookie is the app
# session cookie, additionally pinned by `stack_id` so a cookie minted for one
# host can never be replayed against another.
# ---------------------------------------------------------------------------


def stack_platform_client_id() -> str:
    """The KC client id stacks log in with — the platform's fastapi-app
    client (there is no per-stack client to register)."""
    return KEYCLOAK_AUDIENCE


def stack_redirect_uri(stack: models.PersistentStack) -> str:
    """The callback redirect URI pinned to this stack's serving host.

    The host is the one `get_stack_by_hostname` matched (public_hostname first,
    then custom_domain / friendly_fqdn — the same trio the ingress renders a
    cert for), so the OIDC redirect can never point at a host the stack does
    not serve. Always https: stack ingress is TLS-issued by construction
    (the on-demand `ask` seam).
    """
    return (f"https://{_stack_serving_host(stack)}"
            f"{app_forward_auth.MOD_AUTH_PATH}/callback")


def _stack_serving_host(stack: models.PersistentStack) -> str:
    """The stack's canonical serving host (public_hostname wins)."""
    for name in (getattr(stack, "public_hostname", None),
                 getattr(stack, "custom_domain", None),
                 getattr(stack, "friendly_fqdn", None)):
        name = str(name or "").strip().lower()
        if name:
            return name
    return ""


def stack_login_url_for(stack: models.PersistentStack,
                        redirect_uri: str) -> Tuple[str, str, str]:
    """Start a PKCE code flow for this stack host: returns
    ``(authorize_url, state, verifier)``.

    Fresh PKCE per attempt (verifier never leaves the signed state cookie, the
    authorize URL only carries its S256 challenge). ``redirect_uri`` must be
    this stack's own host — the caller passes `stack_redirect_uri(stack)`; a
    foreign URI would be an open redirect, so it is refused here too.
    """
    expected = stack_redirect_uri(stack)
    if (redirect_uri or "").strip() != expected:
        return "", "", ""
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(24)
    url = app_forward_auth.kc_authorize_url(
        stack_platform_client_id(), redirect_uri, state,
        app_forward_auth.pkce_challenge(verifier))
    return url, state, verifier


def stack_state_cookie(stack: models.PersistentStack, rd: str) -> str:
    """The short-lived signed auth-state cookie for a stack login.

    Reuses the app-side mint (app_id slot carries the STACK id — the callback
    parses it back with `stack.id`, so a state cookie started on another host
    is refused) with `rd` forced relative (open-redirect fence).
    """
    rd = rd if app_forward_auth._RD_RE.match(rd or "/") else "/"
    return app_forward_auth.mint_state_cookie(int(stack.id), rd)


def stack_session_token(stack: models.PersistentStack, claims: dict) -> str:
    """Mint the stack's session cookie (same HS256 JWT as apps, plus a
    `stack_id` pin the verify endpoint re-checks against the inbound host)."""
    return app_forward_auth.mint_session_token(
        {
            "app_id": int(stack.id),
            "stack_id": int(stack.id),
            "tenant_id": stack.tenant_id,
            "sub": claims.get("sub"),
            "email": claims.get("email") or "",
            "groups": claims.get("groups") or [],
            "user_type": claims.get("user_type")
                         or app_forward_auth.user_type_for(
                             None, None, None, claims),
        },
        app_forward_auth.session_ttl_seconds(),
    )


def mint_stack_assertion(*, stack, user, claims, groups, user_type,
                         ttl_seconds: int
                         = app_forward_auth.ASSERTION_TTL_SECONDS) -> str:
    """Mint the signed X-Mod-Assertion for a STACK host (30.6.4 c4d).

    Same shared core as the app family (app_forward_auth._mint_assertion), but
    aud is pinned to `stack:<id>` and the kind claim is `stack_id` — never
    `app_id`. tenant_id is read from the stack's DB row, NEVER from claims
    (S1): claims carrying a forged tenant_id cannot move the pin. Raises
    AppAssertionError on an unusable sub / oversized token, RuntimeError from
    _private_key() when the manager keypair is unset — the caller logs stack
    id + error class only and replies without the header (S5, never the token).
    """
    return app_forward_auth._mint_assertion(
        aud=f"stack:{int(stack.id)}",
        subject_claims={"stack_id": int(stack.id)},
        tenant_id=stack.tenant_id,
        user=user, claims=claims, groups=groups, user_type=user_type,
        ttl_seconds=ttl_seconds)
