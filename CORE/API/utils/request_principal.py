"""The current request's principal, reachable from code that was never given it.

`utils.llm_client.call_wizard_llm` takes prompts and config and nothing else —
no user, no tenant — across 17 call sites spread through node_builder. Anything
that needs to know WHO is making an LLM call therefore has three options:

  1. thread a user through all 17 signatures (invasive, and one missed site is
     silently unlimited),
  2. wrap at the router layer (fewer edits, but "did we find every router that
     eventually reaches an LLM" is harder to answer than it sounds), or
  3. put the principal in a request-scoped context and read it where it is
     needed.

This is (3). A ContextVar is set where auth already resolves the principal, and
read at the LLM boundary.

WHY A CONTEXTVAR AND NOT A GLOBAL. FastAPI runs each request in its own context,
and ContextVars propagate into `await`ed coroutines and into threadpool workers
started by FastAPI for sync endpoints. A module-level global would be shared
across concurrently handled requests and would attribute one tenant's call to
another.

WHAT IS DELIBERATELY NOT COVERED. Background work — APScheduler jobs, the
long-running daemon threads started at boot — runs outside any request, so the
context is empty there. That is the correct outcome rather than a gap to patch:
those paths run as the service account, which every consumer of this module
already exempts. An empty context therefore means "system work", and callers
must treat it that way rather than guessing at a tenant.
"""
from __future__ import annotations

import contextvars
from typing import Any, Optional

# Holds the resolved principal for the current request, or None outside one.
_current_principal: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar(
    "mod_current_principal", default=None)


def set_principal(user: Any) -> contextvars.Token:
    """Record the principal for this request. Returns the token needed to
    reset it, which matters in tests and in any code that sets it manually."""
    return _current_principal.set(user)


def reset_principal(token: contextvars.Token) -> None:
    try:
        _current_principal.reset(token)
    except (ValueError, LookupError):
        # Resetting a token from a different context is not worth raising over.
        pass


def get_principal() -> Optional[Any]:
    """The current request's principal, or None when there is no request —
    background jobs, startup tasks, and tests that never set one.

    None means SYSTEM, not "unknown tenant". Callers must not invent a tenant
    from its absence.
    """
    return _current_principal.get()


def current_tenant_id() -> Optional[int]:
    """Convenience for the common case. None outside a request."""
    user = get_principal()
    return getattr(user, "tenant_id", None) if user is not None else None


# ---------------------------------------------------------------------------
# Channel — how the caller authenticated
# ---------------------------------------------------------------------------
#
# `require_bearer_or_api_key` already distinguishes three cases and then
# discards the distinction:
#
#   1. an `X-API-Key` header                        -> programmatic
#   2. a Bearer token whose `azp` starts `api-key-` -> programmatic
#   3. a Bearer token from a Keycloak user session  -> interactive
#
# Case 2 is the trap. Treating "has an Authorization: Bearer header" as
# interactive misclassifies an API key that happens to be carried as a token,
# which is exactly how a machine integration authenticates against this API.
#
# Recorded even though nothing prices on it yet: programmatic and interactive
# load have different shapes and different growth, and neither can be priced or
# capacity-planned before the split has been measured.

CHANNEL_PROGRAMMATIC = "programmatic"
CHANNEL_INTERACTIVE = "interactive"
CHANNEL_UNKNOWN = "unknown"

_current_channel: contextvars.ContextVar[str] = contextvars.ContextVar(
    "mod_current_channel", default=CHANNEL_UNKNOWN)


def set_channel(channel: str) -> contextvars.Token:
    return _current_channel.set(channel)


def reset_channel(token: contextvars.Token) -> None:
    try:
        _current_channel.reset(token)
    except (ValueError, LookupError):
        pass


def get_channel() -> str:
    """How the current caller authenticated, or "unknown" outside a request."""
    return _current_channel.get()


def classify_channel(x_api_key: Optional[str], azp: Optional[str]) -> str:
    """Programmatic or interactive, from the two signals auth already has.

    `azp` is the Keycloak authorized-party claim; API keys issued by this
    platform present as clients named `api-key-<id>`.
    """
    if x_api_key:
        return CHANNEL_PROGRAMMATIC
    if azp and str(azp).startswith("api-key-"):
        return CHANNEL_PROGRAMMATIC
    if azp:
        return CHANNEL_INTERACTIVE
    return CHANNEL_UNKNOWN


# ---------------------------------------------------------------------------
# API key — WHICH key authorised this request (unified-authorization Gates 32-36)
# ---------------------------------------------------------------------------
#
# A request authenticated by an API key carries the ApiKey principal here so code
# far from the auth boundary can (a) attribute the action to the specific key in
# the audit trail (Gate 33) and (b) cap the request's effective capabilities by
# the key's scope in authorize() (Gate 35/36) — independently of how the user
# object was obtained. This matters because cog checks re-query the user by id
# and hand authorize() a fresh User with no attribute set on the principal
# object, so the key cannot ride on the user. None means "not an API-key request"
# (a browser session, or system work) and imposes no scope restriction.

_current_api_key: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar(
    "mod_current_api_key", default=None)


def set_api_key(api_key: Any) -> contextvars.Token:
    """Record the ApiKey that authorised this request."""
    return _current_api_key.set(api_key)


def reset_api_key(token: contextvars.Token) -> None:
    try:
        _current_api_key.reset(token)
    except (ValueError, LookupError):
        pass


def get_api_key() -> Optional[Any]:
    """The ApiKey for this request, or None for a session / system caller."""
    return _current_api_key.get()


# ---------------------------------------------------------------------------
# App — which app's key authorised this request (GX.A3b)
# ---------------------------------------------------------------------------
#
# A request authenticated by an APP key (api_keys.app_id set) carries the app
# id here so the audit trail attributes the action to THE APP, not only to the
# key's backing service user. Set once in dependencies._enforce_api_key_policy
# — the single funnel every resolved API key passes through — and read by the
# audit writers to stamp ``app_id`` into the row metadata. None means "not an
# app-key request".

_current_app_id: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "mod_current_app_id", default=None)


def set_app_id(app_id: Optional[int]) -> contextvars.Token:
    """Record the App id that authorised this request."""
    return _current_app_id.set(app_id)


def reset_app_id(token: contextvars.Token) -> None:
    try:
        _current_app_id.reset(token)
    except (ValueError, LookupError):
        pass


def get_app_id() -> Optional[int]:
    """The App id for this request, or None for a non-app-key caller."""
    return _current_app_id.get()
