"""
mod_node_auth — worker-mediated node credential helper.

Vendored (COPYed) into each node container. One file, identical everywhere.
Node code imports this instead of reading ``API_KEY`` inline.

Drop-in replacement for the historical inline pattern::

    API_KEY = os.environ.get("API_KEY", "")
    def get_headers():          return {"X-API-Key": API_KEY, "Content-Type": "application/json"}
    def get_service_headers():  return {"X-API-Key": API_KEY}

Public API (exact names — keep migrations uniform)::

    get_headers()          -> {"X-API-Key": <token>, "Content-Type": "application/json"}
    get_service_headers()  -> {"X-API-Key": <token>}
    refresh()              -> None   # drop cached token so the next call refetches

Token acquisition (``<token>``):

* If ``MOD_TASK_TOKEN_URL`` is set, POST to it with header
  ``X-Mod-Task-Nonce: $MOD_TASK_NONCE`` and read ``{"token", "exp"}`` from the
  JSON body. The token is cached in-process and reused until a safety margin
  (``SAFETY_MARGIN_SECONDS``, default 30s) before ``exp``, then refetched.
* DUAL-READ fallback (transitional): if ``MOD_TASK_TOKEN_URL`` is NOT set but
  ``API_KEY`` IS set, use ``API_KEY`` directly so not-yet-migrated deployments
  keep working.
* If neither is available, headers carry an empty key (the caller will 401 —
  acceptable, identical to today's behaviour).

401 retry pattern — implemented by ``request_with_refresh()`` (one refresh +
retry on a 401, never a third attempt)::

    from mod_node_auth import request_with_refresh
    resp = request_with_refresh("GET", url, timeout=10)          # get_headers()
    resp = request_with_refresh("POST", url, json=payload, service=True,
                                timeout=30)                     # get_service_headers()

Equivalent to the manual pattern::

    resp = requests.get(url, headers=mod_node_auth.get_headers())
    if resp.status_code == 401:
        mod_node_auth.refresh()                       # clear the cached token
        resp = requests.get(url, headers=mod_node_auth.get_headers())  # retry once

Importable on Python 3.9+. Uses ``requests`` (already present in node
containers) for the token fetch; the fetch is isolated in one function so it
can be mocked in tests without a live worker.
"""

import os
import threading
import time

__all__ = ["get_headers", "get_service_headers", "refresh", "request_with_refresh"]

# Refetch this many seconds BEFORE the token's ``exp`` so an in-flight call
# never rides an about-to-expire token.
SAFETY_MARGIN_SECONDS = 30

# Network timeout for the worker token fetch (seconds).
_FETCH_TIMEOUT_SECONDS = 10

_lock = threading.Lock()
_cached_token = None          # type: str | None
_cached_exp = 0.0             # unix seconds; 0 == nothing cached


def _now():
    return time.time()


def _fetch_from_worker(url, nonce):
    """POST to the worker token endpoint and return ``(token, exp)``.

    Isolated so tests can monkeypatch it without touching the network.
    """
    import requests  # imported lazily; node containers already ship it

    resp = requests.post(
        url,
        headers={"X-Mod-Task-Nonce": nonce or ""},
        timeout=_FETCH_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["token"], float(data["exp"])


def _get_token():
    """Return the current bearer token string ("" if none is obtainable)."""
    url = os.environ.get("MOD_TASK_TOKEN_URL", "")

    # Transitional dual-read: no worker endpoint, but a static API_KEY exists.
    if not url:
        return os.environ.get("API_KEY", "")

    global _cached_token, _cached_exp
    with _lock:
        if _cached_token is not None and _now() < (_cached_exp - SAFETY_MARGIN_SECONDS):
            return _cached_token

        nonce = os.environ.get("MOD_TASK_NONCE", "")
        token, exp = _fetch_from_worker(url, nonce)
        _cached_token = token
        _cached_exp = exp
        return token


def refresh():
    """Drop the cached token so the next header call refetches from the worker.

    Call this after receiving a 401, then retry the request once (see the
    module docstring). A no-op when running in dual-read (API_KEY) mode.
    """
    global _cached_token, _cached_exp
    with _lock:
        _cached_token = None
        _cached_exp = 0.0


def get_headers():
    return {"X-API-Key": _get_token(), "Content-Type": "application/json"}


def get_service_headers():
    return {"X-API-Key": _get_token()}


def request_with_refresh(method, url, service=False, **kwargs):
    """Issue an HTTP request; on a 401, refresh() once and retry once.

    Implements the module docstring's 401 retry pattern. Callers must NOT pass
    ``headers=`` — this function builds them (``get_service_headers()`` when
    ``service`` is true, else ``get_headers()``). At most two attempts: a second
    401 is returned as-is, never a third try.

    Security MUSTs: the token is never logged here; there is no retry loop; a
    401 never falls back from the task token to a static key; TLS verification
    is never disabled (callers passing ``verify=False`` are rejected).
    """
    if "headers" in kwargs:
        raise ValueError("request_with_refresh builds the auth headers; do not pass headers=")
    if kwargs.get("verify", True) is False:
        raise ValueError("request_with_refresh never disables TLS verification")

    import requests  # imported lazily; node containers already ship it

    headers = get_service_headers() if service else get_headers()
    resp = requests.request(method, url, headers=headers, **kwargs)
    if resp.status_code != 401:
        return resp

    refresh()  # drop the cached token so the retry refetches from the worker
    headers = get_service_headers() if service else get_headers()
    return requests.request(method, url, headers=headers, **kwargs)
