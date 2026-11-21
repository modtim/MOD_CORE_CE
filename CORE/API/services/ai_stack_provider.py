"""AI Stack Provider — resolves the "MOD Stack" provider choice on an
AI-Configuration slot (Node Wizard builder / review / learning-store
embedding, Container Adapter, AI Assistant chat / User-RAG embedding /
MOD-docs embedding, Build Repair) to a running `PersistentStack`'s reachable
endpoint + served model name.

GE.AIC.1-3 CONTRACT
--------------------
Every AI-Configuration slot's Vault-stored config
(`platform/ai-config/<tool>`, projected by `routers/ai_config.py`'s
`ToolAIConfig`) may set `provider == "stack"`. When it does, two more keys
govern resolution, stored in the SAME Vault JSON blob (no new DB columns,
no migration):

    stack_id      int   — a `PersistentStack.id` belonging to the caller's
                          own tenant (matches how `crud/persistent_stacks_domain
                          .list_stacks_by_tenant` scopes everything else the
                          Persistent Stacks page shows that tenant).
    served_model  str   — which of that stack's served model names to bind
                          to. Optional when the stack serves exactly one
                          model (auto-picked); required — and validated —
                          when it serves more than one.

Embedding slots reuse their EXISTING model-name field as `served_model`
(`embedding_model` for the wizard learning store, `embedding_model_user_rag`
/ `embedding_model_docs` for the assistant's two stores) rather than adding a
parallel field, and gain a sibling provider switch:
`embedding_provider` / `embedding_stack_id` (wizard), and
`embedding_provider_user_rag` / `embedding_stack_id_user_rag` +
`embedding_provider_docs` / `embedding_stack_id_docs` (assistant, one pair
per store). Provider value is `"platform"` (today's self-hosted-from-Models-
tab default) or `"stack"`.

Two entry points every consumer goes through:

  * `resolve_stack_endpoint(db, stack_id, served_model, tenant_id=None)`
    — CALL-TIME resolution for a CHAT slot. Returns `(base_url, model_name)`
    or raises `StackProviderError` (a clear, user-facing message naming the
    stack) for: stack not found / out of tenant scope, stack not `running`,
    an unreachable endpoint, or a `served_model` the stack does not actually
    serve. There is NO silent fallback to another model here — a slot's own
    `fallback_enabled`/`fallback_tool` is the only thing allowed to
    substitute another binding, and only because the operator opted in.
    Used by `routers/ai_config.py::_resolved_effective_env` — the single
    chokepoint `utils/llm_client.py`, `services/assistant_orchestrator.py`
    and `services/support_chat_shared_service.py` all resolve a tool's LLM
    binding through — so wiring it in there covers every chat consumer
    (wizard builder, wizard review/critic, container adapter, AI Assistant
    chat, build repair) in one place.

  * `embed_via_stack(base_url, model_name, texts)` — CALL-TIME resolution
    for an EMBEDDING slot: POSTs the OpenAI-compatible `/v1/embeddings`
    endpoint and returns vectors in request order. Used by
    `utils/embeddings.py` (assistant User-RAG store), `utils/docs_ingester.py`
    (assistant MOD-docs store) and `services/wizard_ml/embeddings.py`
    (wizard learning store) ahead of their existing in-process
    sentence-transformers fallback.

SAVE-TIME validation (`PUT /platform/ai-config/{tool}`) goes through
`validate_stack_choice(db, stack_id, served_model, tenant_id, kind=...)` —
same existence/scope/serves-model checks as `resolve_stack_endpoint`, but it
does NOT require the stack to be running right now (an operator may save a
stack pin before starting it) and additionally checks the stack's `kind`
("chat" | "embedding") agrees with the slot. Raised as `StackProviderError`;
the router turns that into a 422.

`list_stack_options(db, tenant_id)` backs `GET /platform/ai-config/
stack-options`: every one of the tenant's `PersistentStack` rows, projected
to `{stack_id, name, status, kind, served_models}`. `kind` is derived from
`services.llm_idle.served_models()` — no stack metadata declares an
embedding/chat role today, so this is a heuristic on the served model
NAME (`embed`/`bge`/`e5`/`nomic`/`gte` substrings -> "embedding", else
"chat").

Reuses `routers.ai_config._stack_custom_base_url` (a generic "how do I reach
this stack" resolver already used for the managed generator/reasoner pair)
and `services.llm_idle.served_models` rather than re-deriving stack
reachability — both via LAZY imports, matching `routers/ai_config.py`'s own
convention (avoids a circular import: that module already reaches into this
one lazily for other helpers).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from product_names import themed_logger

logger = themed_logger(__name__, 'stacks')

_EMBED_NAME_HINTS = ("embed", "bge", "e5", "nomic", "gte")


class StackProviderError(Exception):
    """A clear, user-facing message naming the stack. Callers surface
    `str(exc)` directly — to an HTTPException(422) at save time, or as the
    consumer's own "can't reach the model" error at call time."""


def _served_models_for(stack) -> List[str]:
    from utils.paid_import import optional_paid
    # GT.D6 DS-4 — `llm_idle` is Paid: CE derives the same names from the row's
    # compose command (--served-model-name, else --alias, else --model) instead.
    _llm_idle = optional_paid("services.llm_idle")
    if _llm_idle is not None:
        return _llm_idle.served_models(stack)
    out: List[str] = []
    services = ((stack.compose_spec or {}).get("services") or {})
    for svc in services.values():
        cmd = (svc or {}).get("command") or []
        if isinstance(cmd, str):
            cmd = cmd.split()
        # --served-model-name / --alias is the id the server actually answers
        # to (llama.cpp: --alias); --model is only the weights path.
        for flag in ("--served-model-name", "--alias", "--model"):
            if flag in cmd:
                i = cmd.index(flag)
                if i + 1 < len(cmd):
                    out.append(str(cmd[i + 1]))
                    break
    return out


def stack_kind(stack) -> str:
    """"embedding" if any served model name looks like an embedding model,
    else "chat". A stack serving nothing is reported "chat" (unknown reads
    as the more common case rather than as a third state UI has to handle)."""
    for name in _served_models_for(stack):
        low = (name or "").lower()
        if any(hint in low for hint in _EMBED_NAME_HINTS):
            return "embedding"
    return "chat"


def _get_stack_in_scope(db: Session, stack_id: Optional[int],
                         tenant_id: Optional[int]):
    if not stack_id:
        return None
    from crud.persistent_stacks_domain import get_stack_by_id
    stack = get_stack_by_id(db, int(stack_id))
    if stack is None:
        return None
    if tenant_id is not None and stack.tenant_id != tenant_id:
        return None
    return stack


def _pick_served_model(stack, served_model: Optional[str]) -> str:
    names = _served_models_for(stack)
    if not names:
        raise StackProviderError(
            f"Stack '{stack.name}' (#{stack.id}) does not declare a served "
            f"model — it can't be used as a provider yet.")
    if served_model:
        if served_model not in names:
            raise StackProviderError(
                f"Stack '{stack.name}' (#{stack.id}) does not serve "
                f"'{served_model}'. It serves: {', '.join(names)}.")
        return served_model
    if len(names) > 1:
        raise StackProviderError(
            f"Stack '{stack.name}' (#{stack.id}) serves multiple models "
            f"({', '.join(names)}) — choose one.")
    return names[0]


def resolve_stack_endpoint(db: Session, stack_id: Optional[int],
                            served_model: Optional[str] = None,
                            tenant_id: Optional[int] = None) -> Tuple[str, str]:
    """(base_url, model_name) for a running stack. Raises StackProviderError
    naming the stack for every failure mode — never returns a partial or
    guessed result."""
    if not stack_id:
        raise StackProviderError("No stack is configured for this slot.")
    stack = _get_stack_in_scope(db, stack_id, tenant_id)
    if stack is None:
        raise StackProviderError(f"Stack #{stack_id} was not found.")
    if stack.status != "running":
        raise StackProviderError(
            f"Stack '{stack.name}' (#{stack_id}) is {stack.status}, not "
            f"running — start it before it can serve this slot.")
    model_name = _pick_served_model(stack, served_model)
    # 1788k CE carve: routers/ai_config* is PAID (the tenant AI-config page), so
    # its `_stack_custom_base_url` helper is absent in CE. There is no tenant AI
    # config override there, so CE resolves the stack endpoint with the same
    # platform-default shape persistent_stacks_domain_service uses for
    # GET /{stack_id}/endpoint: the stack's own lb_endpoint (worker-local host:port).
    from utils.paid_import import optional_paid
    _ai_config = optional_paid("routers.ai_config")
    base_url = (_ai_config._stack_custom_base_url(stack, db)
                if _ai_config is not None
                else (f"http://{stack.lb_endpoint}" if stack.lb_endpoint else None))
    if not base_url:
        raise StackProviderError(
            f"Stack '{stack.name}' (#{stack_id}) has no reachable endpoint "
            f"right now.")
    return base_url, model_name


def validate_stack_choice(db: Session, stack_id: Optional[int],
                           served_model: Optional[str],
                           tenant_id: Optional[int], *,
                           kind: str = "chat") -> Tuple[Any, str]:
    """SAVE-time check: exists, in scope, serves the named model, and is the
    right kind of stack for this slot. Deliberately does NOT require
    `status == running` (an operator may pin a stopped stack ahead of
    starting it). Returns (stack, resolved_served_model) on success."""
    if not stack_id:
        raise StackProviderError(
            "stack_id is required when provider is 'stack'.")
    stack = _get_stack_in_scope(db, stack_id, tenant_id)
    if stack is None:
        raise StackProviderError(
            f"Stack #{stack_id} was not found for this tenant.")
    actual_kind = stack_kind(stack)
    if kind and actual_kind != kind:
        raise StackProviderError(
            f"Stack '{stack.name}' (#{stack_id}) serves {actual_kind} "
            f"models, not {kind} — choose a {kind} stack.")
    model_name = _pick_served_model(stack, served_model)
    return stack, model_name


def list_stack_options(db: Session, tenant_id: int) -> List[Dict[str, Any]]:
    """Every PersistentStack row this tenant owns, projected for the
    Dashboard's Stack picker: `{stack_id, name, status, kind, served_models}`."""
    from crud.persistent_stacks_domain import list_stacks_by_tenant
    out: List[Dict[str, Any]] = []
    for st in list_stacks_by_tenant(db, tenant_id):
        out.append({
            "stack_id": st.id,
            "name": st.name,
            "status": st.status,
            "kind": stack_kind(st),
            "served_models": _served_models_for(st),
        })
    return out


def _stack_target_refusal(url: str) -> Optional[str]:
    """FX.151.p2a1 — SSRF vetting for a resolved stack endpoint.

    ``base_url`` here is NOT free-form tenant input: it comes from
    :func:`routers.ai_config._stack_custom_base_url` for a PersistentStack
    row already scoped to the tenant, so its host is a platform-registered
    service — a MagicDNS ``.ts.net`` name, a docker/service alias, or a
    published port on a worker's LAN address. Those legitimately resolve to
    PRIVATE addresses (on-prem stacks), so the guard's default "refuse
    private" rule must not break them: the caller hands
    :func:`utils.safe_http.request` the resolved host in its explicit
    internal-allow mode (``_ssrf_allowlist``), which also writes the
    ``SSRF_ALLOWLIST_FETCH`` audit row.

    What this pre-check still refuses, and what the allow mode NEVER unlocks:
    non-http(s) schemes, the API's own docker peers
    (:data:`services.ssrf_guard.BLOCKED_INTERNAL_HOSTNAMES`), and the
    SSRF-sensitive address set — loopback, link-local and cloud metadata
    (``169.254.169.254``). An unresolvable host is not refused here (the
    transport fails naturally with its own DNS error, per
    ``utils/safe_http.py``).
    """
    from urllib.parse import urlparse
    from services.ssrf_guard import BLOCKED_INTERNAL_HOSTNAMES
    from utils.safe_http import _resolve
    import ipaddress

    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001 — malformed -> refuse
        return "unparseable url"
    if (parsed.scheme or "").lower() not in ("http", "https"):
        return "only http/https URLs are allowed"
    host = parsed.hostname
    if not host:
        return "no host"
    if host.lower().rstrip(".") in BLOCKED_INTERNAL_HOSTNAMES:
        return f"internal hostname '{host}' is not allowed"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_loopback or ip.is_link_local or ip.is_unspecified):
        return f"{host} is a loopback/link-local address"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    infos = _resolve(host, port)
    if infos is None:
        return None  # unresolvable: the transport fails naturally
    for a in infos:
        try:
            addr = ipaddress.ip_address(a[4][0])
        except ValueError:
            continue
        if addr.is_loopback or addr.is_link_local or addr.is_unspecified:
            return f"{host} resolves to a loopback/link-local address ({addr})"
    return None


def _host_gateway_stack_endpoints() -> set:
    """`host:port` strings the platform itself computed for stacks on the
    EMBEDDED worker (`PersistentStack.lb_endpoint`, built by
    `routers.persistent_stacks._shared._compute_lb_endpoint`). Own session:
    callers are on the LLM call path with no db in hand."""
    import models
    from database import SessionLocal
    from routers.persistent_stacks._shared import _EMBEDDED_STACK_HOST
    db = SessionLocal()
    try:
        rows = (db.query(models.PersistentStack.lb_endpoint)
                .filter(models.PersistentStack.lb_endpoint.like(f"{_EMBEDDED_STACK_HOST}:%"))
                .all())
        return {r[0] for r in rows if r[0]}
    finally:
        db.close()


def is_platform_stack_endpoint(base_url: str) -> bool:
    """True ONLY when `base_url` is exactly an endpoint the platform computed
    for a stack on the embedded worker (host-gateway name + that stack's
    published port) AND it passes :func:`_stack_target_refusal`.

    The ONE definition of "stack-derived host-gateway endpoint" for the LLM
    call guard (utils/llm_client._post). `host.docker.internal` resolves to the
    Docker bridge gateway (private), which the default SSRF rule refuses; a
    stack the user bound in AI Configuration must still work. A user-typed
    BYO URL naming the host gateway on a port no stack published is NOT
    matched (stays refused); loopback/link-local/metadata are refused by
    `_stack_target_refusal` regardless. Never raises."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(base_url or "")
        host = (parsed.hostname or "").lower()
        from routers.persistent_stacks._shared import _EMBEDDED_STACK_HOST
        if host != _EMBEDDED_STACK_HOST or not parsed.port:
            return False
        if _stack_target_refusal(base_url) is not None:
            return False
        return f"{host}:{parsed.port}" in _host_gateway_stack_endpoints()
    except Exception:  # noqa: BLE001 — a lookup fault means "not proven stack-derived"
        logger.debug("stack-endpoint check failed for %r", base_url, exc_info=True)
        return False


def embed_via_stack(base_url: str, model_name: str,
                     texts: List[str], *,
                     tenant_id: Optional[int] = None) -> List[List[float]]:
    """POST {base_url}/v1/embeddings (OpenAI-compatible) — vectors in
    request order. Raises StackProviderError on any failure so callers can
    fall back or surface a clear message rather than crash on a malformed
    response.

    GSEC FX.151.p2a1 — the send goes through the shared guarded client
    (:func:`utils.safe_http.post`): scheme vetting, per-attempt DNS re-check,
    pinned address, manual redirect re-vetting and the size cap. The resolved
    stack host is passed as the guard's explicit internal-allow entry so
    platform-registered private stack endpoints keep working (audited).
    """
    url = f"{base_url.rstrip('/')}/v1/embeddings"
    refusal = _stack_target_refusal(url)
    if refusal:
        raise StackProviderError(f"Stack embedding endpoint refused: {refusal}")
    from urllib.parse import urlparse
    from utils import safe_http

    host = urlparse(url).hostname
    try:
        resp = safe_http.post(
            url, json={"model": model_name, "input": list(texts)},
            _ssrf_connect_timeout=5.0,
            _ssrf_read_timeout=30.0,
            _ssrf_allowlist=[host] if host else None,
            tenant_id=tenant_id,
        )
        resp.raise_for_status()
        data = resp.json()
        items = sorted(data.get("data") or [], key=lambda d: d.get("index", 0))
        if len(items) != len(texts):
            raise StackProviderError(
                f"Stack embedding endpoint {url} returned {len(items)} "
                f"vectors for {len(texts)} inputs.")
        return [item["embedding"] for item in items]
    except StackProviderError:
        raise
    except Exception as e:  # noqa: BLE001
        raise StackProviderError(f"Stack embedding call to {url} failed: {e}")
