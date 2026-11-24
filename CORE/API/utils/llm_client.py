"""Synchronous LLM wrapper keyed on the wizard-tool Vault config.

Used by any API route that needs a fast, user-facing LLM response
(currently: `POST /node_definitions/analyze-script`). Reuses the same
config plumbing as the async imagebuilder / containerizer paths — one
source of truth lives at `routers.ai_config._resolved_effective_env("wizard")`.

Call shape:

    from utils.llm_client import call_wizard_llm

    parsed = call_wizard_llm(
        system_prompt=SYSTEM,
        user_prompt=USER,
        response_format="json",
        timeout_s=30,
    )

Failure modes (all return None / raise LLMClientError — caller decides
what fallback to render to the user):
  - No LLM_BASE_URL configured → LLMClientError("no LLM base URL")
  - HTTP timeout → LLMClientError("timeout")
  - Non-2xx response → LLMClientError with status + body preview
  - response_format="json" but body isn't parseable → LLMClientError
    (after we try to strip ``` fences and trailing prose)
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Literal, Optional

from sqlalchemy.orm import Session

from utils.paid_import import optional_paid

import requests

from utils import safe_http


logger = logging.getLogger(__name__)


class LLMClientError(Exception):
    """Raised when the synchronous LLM call can't produce a usable result.
    Caller is expected to catch and render a conservative fallback."""


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_FIRST_BRACE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)

# 1637b — the CONNECT leg of every wizard LLM POST, separate from the READ
# timeout (`timeout_s`, which defaults to 600s upstream and is a read bound
# only). An endpoint that accepts the TCP connection and drops the SYN — or
# sends headers then stops — used to block the request for the full read
# ceiling (and, in `_persist_wizard_rationale`, hold an open DB transaction
# that long). No platform-settings key fits a connect bound (`wizard.llm_call_
# timeout_seconds` caps the READ leg only), so this is a module constant; the
# lead registers a key and this reads it when present. Never raises: a bad
# setting falls back to the constant, and a non-positive value is ignored.
_LLM_CONNECT_TIMEOUT_SECONDS = 10.0
_LLM_CONNECT_TIMEOUT_SETTING_KEY = "wizard.llm_connect_timeout_seconds"


def _llm_connect_timeout() -> float:
    """Seconds to allow the CONNECT/TLS handshake of a wizard LLM POST."""
    try:
        from services import platform_settings as _ps
        _val = float(_ps.get_setting(_LLM_CONNECT_TIMEOUT_SETTING_KEY))
        if _val > 0:
            return _val
    except Exception:  # noqa: BLE001 — unregistered key / registry fault
        pass
    return _LLM_CONNECT_TIMEOUT_SECONDS

# --------------------------------------------------------------------------
# Transport: direct when the model is on this network, dispatched when it is not
# --------------------------------------------------------------------------
#
# A managed-AI stack on a remote worker is reachable by network alias only on
# that worker's own host. The manager cannot resolve `ollama` and MUST NOT
# learn how to — `executor.go:1337` states the rule for the same reason: "the
# server cannot know what URL each remote worker's containers can reach
# (different NATs, VPNs, hostnames, deployments)". Recording worker addresses
# would work on one LAN and break for every customer whose workers are
# anywhere else, and publishing the model's port would expose a private model
# to the network to solve a problem we do not have.
#
# So the call travels the way all other work does: dispatched to the worker
# that hosts the model, executed there against the alias, answered from the
# task result. The manager learns a worker NAME, never an address.
#
# The trigger is DNS, deliberately. "Does this alias resolve here?" is exact,
# instant and self-configuring: on the manager it does and the direct path is
# unchanged, and where it does not there is nothing to guess about.

# The result poll. Tight because by the time we are polling, the model has
# usually already answered — inference on a short completion is ~200ms and the
# wait is almost entirely scheduling. A 2s granularity added up to 2s of pure
# sleep to a call whose useful work took a fraction of that.
_WORKER_CALL_POLL_S = 0.4

# Must match CORE/DockerContainers/llm-call/call.py.
#
# The task tail is NOT stdout — it is the container's whole log, and Docker
# merges both streams into it. The first real dispatch returned a perfectly
# good completion with the container's own diagnostic lines appended after it,
# and json.loads failed on "Extra data". Fencing the payload keeps the
# diagnostics, which are what made that failure diagnosable in the first place.
_RESPONSE_BEGIN = "---MOD-LLM-RESPONSE-BEGIN---"
_RESPONSE_END = "---MOD-LLM-RESPONSE-END---"


def _extract_response(tail: str) -> str:
    """The fenced payload, or the whole tail when no fence is present.

    The fallback is deliberate: a worker still running an older `llm-call`
    image emits an unfenced payload, and refusing it would turn a rolling
    image update into an outage.
    """
    start = tail.find(_RESPONSE_BEGIN)
    if start == -1:
        return tail.strip()
    start += len(_RESPONSE_BEGIN)
    end = tail.find(_RESPONSE_END, start)
    return (tail[start:end] if end != -1 else tail[start:]).strip()


def _worker_call_image() -> str:
    """Fully qualified, because the worker uses `processing_image` verbatim.

    `resolveProcessingImage` only synthesises a registry when the field is
    empty; a bare `modtim/llm-call` would be resolved against Docker Hub and
    fail to pull. Routed through `utils.registries` so an on-prem deployment
    pointing at its own mirror gets its own image rather than reaching out to
    MOD — the whole point of Gate 13.
    """
    override = os.getenv("LLM_CALL_IMAGE")
    if override:
        return override
    from utils.registries import (
        platform_image, configured_platform_registry,
        DEFAULT_PLATFORM_REGISTRY,
        DEFAULT_PLATFORM_PROJECT,
    )
    try:
        return platform_image("llm-call:latest")
    except Exception:  # noqa: BLE001
        logger.debug("registry resolution failed for llm-call", exc_info=True)
        # Fall back WITHOUT a hardcoded hostname (Gate 12): honour the
        # deployment's DOCKER_REGISTRY/HARBOR_PROJECT, and only if those are
        # unset reach for the resolver's documented default.
        registry = configured_platform_registry().rstrip("/") \
            or DEFAULT_PLATFORM_REGISTRY
        project = (os.getenv("HARBOR_PROJECT") or DEFAULT_PLATFORM_PROJECT).strip("/")
        return f"{registry}/{project}/llm-call:latest"


class _WorkerResponse:
    """Enough of a `requests.Response` that the call path stays transport-blind.

    Everything downstream — metering, usage extraction, content parsing, the
    error branches — reads `.ok`, `.status_code`, `.text` and `.json()`. Giving
    the dispatched path the same shape means one line of this module knows
    which transport ran, instead of every line after it.
    """

    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self.ok = 200 <= status_code < 300

    def json(self):
        return json.loads(self.text)


# A FAILING name lookup is not free: measured at ~3s per call in this
# container, because the resolver waits out its timeouts before returning
# NXDOMAIN. Paying that on every LLM call — for an answer that only changes
# when a stack moves host — was 3 of the 8.4s a dispatched call took.
#
# Short TTL rather than a permanent memo: the answer DOES change, when a stack
# is deployed to or removed from this host, and a stale "resolves" would send
# every call down a direct path to nothing until the process restarted.
_ALIAS_TTL_S = 30.0
_alias_cache: dict = {}


def _alias_resolves(base_url: str) -> bool:
    """Can this process reach the model's hostname directly?"""
    import socket
    import time as _time
    from urllib.parse import urlparse

    host = urlparse(base_url).hostname
    if not host:
        return False

    hit = _alias_cache.get(host)
    if hit is not None and (_time.monotonic() - hit[0]) < _ALIAS_TTL_S:
        return hit[1]

    try:
        socket.getaddrinfo(host, None)
        answer = True
    except socket.gaierror:
        answer = False
    except Exception:  # noqa: BLE001 — never let a name lookup fail the call
        logger.debug("alias resolution check failed for %s", host, exc_info=True)
        return True          # not cached: a fault is not an answer

    _alias_cache[host] = (_time.monotonic(), answer)
    return answer


def _hosting_worker(base_url: str):
    """(worker_name, stack_name, tenant_id) for the stack serving `base_url`.

    The tenant comes off the stack row rather than being assumed. Managed AI is
    platform-owned so it is tenant 1 today, but a literal here would silently
    bill and scope every call to tenant 1 the first time a tenant owns a model
    stack — and that is the kind of wrong that reads as correct for a year.

    Matched on the compose alias, which is what the URL actually names, rather
    than on the role — the role→alias map is the same one that built the URL,
    so going back through it keeps a single source of truth.
    """
    from urllib.parse import urlparse
    import models
    from database import SessionLocal

    host = urlparse(base_url).hostname
    db = SessionLocal()
    try:
        # RUNNING, not merely targeted. An alias that does not resolve has two
        # very different causes: the model is on another host, or the model is
        # off. Only the first is a routing question. Dispatching the second
        # sends a doomed task to a worker whose stack is also down, and turns
        # an instant connection error into a poll-cycle wait for the same
        # failure — which is how the wizard came to report a stopped model as a
        # queue problem.
        for stack in (db.query(models.PersistentStack)
                      .filter(models.PersistentStack.target_worker.isnot(None),
                              models.PersistentStack.status == "running")
                      .all()):
            for svc in ((stack.compose_spec or {}).get("services") or {}).values():
                aliases = (((svc.get("networks") or {}).get("platform") or {})
                           .get("aliases") or [])
                if host in aliases:
                    return stack.target_worker, stack.name, stack.tenant_id
        return None, None, None
    finally:
        db.close()


def _call_candidates(url: str) -> list:
    """Addresses the hosting worker may be able to reach the model on.

    The alias first — it is the primary address and the only one that works
    when the task container shares the stack's docker network. Then loopback
    on the same port, which is what a host-networked container can reach
    because the stack publishes to 127.0.0.1.

    Both, always, in that order. Choosing between them would mean knowing the
    worker's network mode, which is exactly the knowledge this module refuses
    to hold.
    """
    from urllib.parse import urlparse, urlunparse

    parts = urlparse(url)
    if not parts.hostname or parts.hostname in ("127.0.0.1", "localhost"):
        return [url]
    loopback = parts._replace(
        netloc=f"127.0.0.1:{parts.port}" if parts.port else "127.0.0.1")
    return [url, urlunparse(loopback)]


def _post_via_hosting_worker(url: str, body: dict, api_key: str,
                             timeout_s: float, base_url: str,
                             host_info=None) -> _WorkerResponse:
    """Run one LLM call on the worker that holds the model, and wait for it.

    Raises the same exceptions the direct path does, so the existing timeout
    and failure branches keep working unchanged.
    """
    import time
    import models
    from database import SessionLocal

    # Reuse the caller's lookup when it has one — `_post` already asked, and
    # asking twice per call is a second round trip for an unchanged answer.
    worker, stack_name, tenant_id = host_info or _hosting_worker(base_url)
    if not worker:
        raise requests.RequestException(
            f"{base_url} does not resolve here and no worker reports hosting it")

    # TWO candidate addresses, because the manager cannot know how this worker
    # is wired and must not have to. `docker_network` defaults to "host"
    # (worker.example.yaml:32), so the task container may share the stack's
    # docker network — where the alias resolves — or run in the host namespace,
    # where only the stack's loopback-published port is reachable. Offering
    # both lets the worker's own topology decide, and keeps this module free of
    # any assumption about a host it will never see.
    #
    # Found the only way it could be: the first real dispatch reached
    # worker-example-host and died on `[Errno -3] Temporary failure in name
    # resolution`, with 94 green tests behind it.
    env = {"PLATFORM_TASK": "1", "LLM_URL": ",".join(_call_candidates(url)),
           "LLM_BODY": json.dumps(body), "LLM_TIMEOUT": str(int(timeout_s))}
    if api_key:
        env["LLM_API_KEY"] = api_key

    task_queue = "CPU"
    db = SessionLocal()
    try:
        task = models.ApiTaskQueue(
            node_id=f"llm-call-{stack_name}",
            tenant_id=tenant_id,
            queue_name=task_queue,
            priority=9,
            task_type="system",
            priority_class="normal",
            task_payload={
                "container_env": env,
                "node_kind": "llm-call",
                "processing_image": _worker_call_image(),
                # No MinIO round trip. The completion is kilobytes and the
                # processing path keeps a 5000-line tail — the same channel the
                # Containerizer already returns its adapter JSON on.
                "skip_download": True,
                "skip_upload": True,
            },
            status=models.ApiTaskStatus.ASSIGNED,
            assigned_to_worker=worker,
            max_attempts=1,
            output_namespace="attempt_0",
        )
        db.add(task)
        db.commit()
        task_id = task.id
        logger.info("llm call dispatched to %s as task %s (%s)",
                    worker, task_id, stack_name)
    finally:
        db.close()

    # RING THE BELL. The worker's long poll parks on this Redis channel and
    # wakes instantly on a publish; without one it sleeps out
    # MOD_WORKER_POLL_BACKSTOP_SECS (10s default) before it looks again. Every
    # other dispatcher in the codebase publishes and this one did not, which is
    # most of why the first working call took 20 seconds for 200ms of
    # inference. Fire-and-forget by design — a Redis hiccup costs latency, and
    # the backstop still delivers the task.
    try:
        from routers.worker_tasks import _publish_event
        _publish_event(f"task:available:{task_queue}")
    except Exception:  # noqa: BLE001
        logger.debug("llm call: task-available publish failed", exc_info=True)

    # Poll to a wall clock, not a loop count: the wait is one poll cycle plus
    # inference, and inference is the part that varies.
    deadline = time.monotonic() + timeout_s
    terminal = (models.ApiTaskStatus.COMPLETED, models.ApiTaskStatus.FAILED,
                models.ApiTaskStatus.CANCELLED, models.ApiTaskStatus.EXPIRED)
    while True:
        time.sleep(_WORKER_CALL_POLL_S)
        db = SessionLocal()
        try:
            row = db.get(models.ApiTaskQueue, task_id)
            status = row.status if row else None
            result = dict(row.result_payload or {}) if row else {}
            err = (row.error_message or "") if row else ""
        finally:
            db.close()

        if status in terminal:
            break
        if time.monotonic() > deadline:
            raise requests.Timeout(
                f"llm call task {task_id} on {worker} did not finish in {timeout_s}s")

    if status != models.ApiTaskStatus.COMPLETED:
        raise requests.RequestException(
            f"llm call on {worker} ended {getattr(status, 'value', status)}: "
            f"{err[:300] or 'no error recorded'}")

    tail = (result.get("log_tail") or "").strip()
    if not tail:
        raise requests.RequestException(
            f"llm call on {worker} completed but returned no output")
    return _WorkerResponse(_extract_response(tail))


def _post(url: str, body: dict, headers: dict, timeout_s: float,
          base_url: str, api_key: str):
    """One LLM POST, by whichever transport can actually reach the model."""
    # GSEC FX.19 R-042 / GY.C8 — external LLM egress gate. Local/stack
    # endpoints are unaffected; a refused external target fails with the
    # refusal reason instead of sending the prompt.
    from utils import egress_policy
    external = egress_policy.url_is_external(base_url or url)
    # Cloud-metadata literals are "internal" to url_is_external (link-local), so
    # they would take the direct (unguarded) client below: refuse them outright.
    from urllib.parse import urlparse as _mup
    if (_mup(url).hostname or "").lower() in ("169.254.169.254", "fd00:ec2::254"):
        raise LLMClientError(
            "LLM endpoint refused by the SSRF guard (cloud metadata address)")
    # D76 — an endpoint the platform computed for a stack on the embedded
    # worker (host-gateway name + that stack's published port) is the user's
    # own local stack, not external egress: skip the R-042 external-LLM gate and
    # hand the guard the exact host through its explicit internal-allow mode.
    stack_derived = False
    if external:
        try:
            from services.ai_stack_provider import is_platform_stack_endpoint
            stack_derived = is_platform_stack_endpoint(base_url or url)
        except Exception:  # noqa: BLE001
            stack_derived = False
    if external and not stack_derived:
        reason = egress_policy.refuse_reason(egress_policy.CATEGORY_EXTERNAL_LLM)
        if reason:
            logger.warning("R-042: wizard LLM call refused (%s)", reason)
            raise LLMClientError(reason)
    # GSEC FX.13 R-059 — internal-hop TLS enforcement. off (default)
    # returns verify=True and no check, i.e. unchanged behaviour.
    from utils import internal_tls
    tls_kwargs = {}
    if not external:
        internal_tls.check_internal_url(url)
        verify = internal_tls.internal_verify_arg()
        if verify is not True:
            tls_kwargs["verify"] = verify
    def _send():
        # GSEC FX.151 p1c — external LLM targets (user-supplied base_url)
        # route through the SSRF guard; internal/loopback (local Ollama /
        # vLLM / MagicDNS stack) stay on the direct client so on-prem stacks
        # keep working.
        # 1637b — the CONNECT leg gets its OWN bound, never the read ceiling.
        # `timeout_s` is a READ timeout (default 180s here, 600s upstream); an
        # endpoint that accepts the TCP socket and drops the SYN — or sends
        # nothing at all — used to wait out the whole read ceiling on both
        # transports (`_ssrf_connect_timeout=timeout_s` and the scalar
        # `timeout=timeout_s`, which urllib3 applies to BOTH legs). A tuple
        # `(connect, read)` keeps the connect bound short so the request fails
        # fast instead of hanging the caller.
        _connect = min(_llm_connect_timeout(), float(timeout_s))
        if external:
            # D76 — a stack the platform itself resolved on the embedded worker
            # is dialled by the host-gateway name (private bridge address).
            # Hand ONLY that exact endpoint to the guard's explicit
            # internal-allow mode (loopback/link-local/metadata still refused,
            # audited). A user-typed BYO URL is never matched.
            _allow = None
            if stack_derived:
                from urllib.parse import urlparse as _up
                _h = _up(url).hostname
                _allow = [_h] if _h else None
            try:
                return safe_http.post(url, json=body, headers=headers,
                                      _ssrf_connect_timeout=_connect,
                                      _ssrf_read_timeout=timeout_s,
                                      _ssrf_allowlist=_allow, **tls_kwargs)
            except safe_http.SsrfBlocked:
                from urllib.parse import urlparse
                _host = urlparse(url).hostname
                logger.warning("wizard LLM endpoint refused by SSRF guard (host=%s)", _host)
                raise LLMClientError(
                    f"LLM endpoint '{_host}' refused by the SSRF guard "
                    "(must be a public host)")
        return requests.post(url, json=body, headers=headers,
                             timeout=(_connect, timeout_s), **tls_kwargs)

    if _alias_resolves(base_url):
        return _send()

    # Unresolvable AND nothing running claims to serve it: the model is down,
    # not elsewhere. Take the direct path so it fails immediately and in the
    # way every caller already handles, rather than waiting out a poll cycle
    # for a task that cannot succeed either.
    try:
        host_info = _hosting_worker(base_url)
    except Exception:  # noqa: BLE001 — a lookup fault must not lose the call
        logger.debug("hosting-worker lookup failed for %s", base_url, exc_info=True)
        host_info = None
    if not host_info or not host_info[0]:
        return _send()

    return _post_via_hosting_worker(url, body, api_key, timeout_s, base_url,
                                    host_info=host_info)


def _strip_json_fences(text: Optional[str]) -> str:
    """Some models wrap output in ```json ... ``` even when asked not to.
    Peel the first fenced block; fall back to the first {...} / [...]
    block; return the raw text if nothing matches.

    Defensive on ``text``: OpenAI-compatible providers occasionally
    return ``"content": null`` (tool-call-only responses, empty
    completions, content filter trips). Treat None / non-string as
    empty so the downstream JSON-decode raises a clean LLMClientError
    via the wrapper, not a TypeError 4 frames deep in this regex call.
    Repro 2026-05-31 spec=12: a single hiccup on attempt 5 crashed
    the repair endpoint with 500, killing the whole 10-attempt budget.
    """
    if not isinstance(text, str) or not text:
        return ""
    m = _JSON_FENCE.search(text)
    if m:
        return m.group(1).strip()
    m = _FIRST_BRACE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def _get_tool_config(tool: str) -> dict:
    """Resolve any AI tool's effective LLM env. Isolated so tests can
    monkeypatch one function instead of the whole ai_config module.
    GT.P2.6a: routes through utils.tool_config, which delegates to the paid
    resolver on Pro and reads the tool's Vault env on CE."""
    from utils.tool_config import resolved_effective_env
    return resolved_effective_env(tool)


def normalize_openai_base_url(base_url: Optional[str]) -> str:
    """Strip trailing slashes and ONE trailing ``/v1`` from a stored base URL.

    The convention is a base WITHOUT ``/v1`` (this module appends
    ``/v1/chat/completions``), but an OpenAI-style BYO form entry is naturally
    ``http://host:port/v1``. Left as typed that becomes
    ``/v1/v1/chat/completions`` and the endpoint answers 404 (llama.cpp:
    ``File Not Found``) although the same config reads back as reachable.
    """
    url = (base_url or "").strip().rstrip("/")
    if url.lower().endswith("/v1"):
        url = url[:-3].rstrip("/")
    return url


def _get_wizard_config() -> dict:
    """Resolve the wizard tool's LLM config."""
    return _get_tool_config("wizard")


def _repair_tool_or_wizard() -> str:
    """The repair step uses the 'repair' tool's model when an admin has
    configured one (e.g. pointed it at gpt-oss-20b); otherwise it
    follows the 'wizard' tool, so repair never silently diverges from
    generation unless explicitly opted in. Fail-safe to 'wizard'."""
    try:
        from utils.tool_config import read_tool_config
        return "repair" if read_tool_config("repair") else "wizard"
    except Exception:
        return "wizard"


def _bound_stack_asleep(db: "Session", res: Dict[str, Any]) -> bool:
    """ai-config-stack-binding 4.1 — is the bound stack ASLEEP (design D4)?

    Mirrors ``assistant_orchestrator._bound_stack_asleep`` /
    ``_adapter_bound_stack_asleep`` but scoped to the repair/critic path.
    A stack whose desired_state is ``stopped`` (the platform's sleep policy
    wrote it) is woken; a stack that is merely stopped/not-running with
    desired_state still ``running`` is an operator stop — no wake.
    """
    target = res.get("target") or {}
    stack_id = target.get("stack_id")
    if not stack_id:
        return False
    try:
        from crud.persistent_stacks_domain import get_stack_by_id
        from utils.accelerator_fit import ACTIVE_STACK_STATUSES
        row = get_stack_by_id(db, int(stack_id))
        if row is None or (getattr(row, "status", None) in ACTIVE_STACK_STATUSES):
            return False
        if getattr(row, "desired_state", None) != "stopped":
            return False
        return (getattr(row, "status", None)
                not in ("torn_down", "created", "failed", "provisioning_failed"))
    except Exception:  # noqa: BLE001 — a diagnostic read never breaks the call
        return False


def _slot_failure_cause(slot: str,
                        db: Optional[Session] = None,
                        tenant_id: Optional[int] = None) -> Optional[str]:
    """ai-config-stack-binding 3.4 — the resolver's sentence for why a slot
    will not serve, for EVERY non-`ready` outcome.

    :func:`_resolved_effective_env` (via :func:`_get_tool_config`) degrades a
    failed resolution to ``{}`` — that is its contract, so the dict can never
    leak a diagnostic key into a request/task env. The CAUSE lives in the
    typed resolver (:func:`services.ai_slot_resolver.resolve_slot`), which is
    what every consumer that reports to a human must call directly:
    ``no_binding | stack_stopped | stack_missing | unreachable | waking``.
    Each message already names the slot and the cause (stack name + status,
    or ``host:port``) and the remedy; the fallback chain's other causes are
    appended so "primary stopped, fallback missing" is one read.

    Returns ``None`` only when the slot unexpectedly resolves ``ready`` or the
    resolver cannot speak at all (no table / resolver raised) — the sole case
    where a caller may still fall back the pre-launch way.
    """
    if db is None:
        try:
            from database import SessionLocal
            db = SessionLocal()
        except Exception:  # noqa: BLE001 — no DB in play (unit contexts)
            db = None
    try:
        from services.ai_slot_resolver import REMEDY, resolve_slot
        res = resolve_slot(db, slot, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 — a diagnostic must not break the call
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
    if (res.get("outcome") or "") == "ready":
        return None
    # ai-config-stack-binding 4.1 (design D4) — a bound stack that is ASLEEP
    # is not a failure: start it and serve from it.  Only platform-slept
    # stacks are woken (checked via _bound_stack_asleep); an operator-stopped
    # stack is not woken.  On wake failure we keep the existing fallback /
    # error path (no hang).
    if (res.get("outcome") or "") == "stack_stopped" and _bound_stack_asleep(
            db, res):
        try:
            res = resolve_slot(db, slot, tenant_id=tenant_id, wake=True)
        except Exception:  # noqa: BLE001 — wake failure falls through
            pass
        if (res.get("outcome") or "") == "ready":
            return None
    message = (res.get("message") or "").strip()
    attempts = res.get("attempts") or []
    if len(attempts) > 1:
        others = [(a.get("message") or "").replace(REMEDY, "").strip()
                  for a in attempts[1:]]
        others = [o for o in others if o]
        if others and message:
            message = f"{message} Fallbacks: {' '.join(others)}"
    return message or None


def _repair_slot_for(
    db: Optional["Session"] = None,
    tenant_id: Optional[int] = None,
) -> tuple:
    """Which slot the wizard build's REPAIR proposer runs on, plus the
    resolver's cause sentence when that slot will not serve.

    ai-config-stack-binding 3.4 — the old ``_repair_tool_or_wizard()``
    silently followed the *wizard* slot when the repair slot had no
    configured model, so a stopped/missing/unreachable repair binding was
    hidden behind a different model's answer. Now the slot is the slot:
    the repair slot is always the one consulted, and a non-``ready``
    outcome yields its named cause instead of a substitution.

    Returns ``(slot, cause_or_None)``; ``cause`` is non-None only when the
    slot itself failed.
    """
    if _get_tool_config("repair"):
        # The repair slot can name an endpoint: either it resolved ``ready``
        # through a binding row, or nobody has picked the slot yet and its
        # own transitional Vault blob still answers (the same slot — never
        # another model, never another slot).
        return "repair", None
    cause = _slot_failure_cause("repair", db=db, tenant_id=tenant_id)
    if cause is None:
        # The repair slot is available (configured, possibly woken from
        # platform sleep): use it even if _get_tool_config was falsy
        # (transitional path — Vault blob still answers).
        return "repair", None
    if cause:
        return "repair", cause
    # The resolver had nothing to name (no binding table in play, resolver
    # raised): keep the historical pairing, and name the failure of the slot
    # that pairing lands on rather than substituting a third model.
    tool = _repair_tool_or_wizard()
    return tool, _slot_failure_cause(tool, db=db, tenant_id=tenant_id)


def _call_wizard_llm_impl(
    system_prompt: str,
    user_prompt: str,
    *,
    response_format: Literal["json", "text"] = "text",
    timeout_s: int = 180,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    json_schema: Optional[dict] = None,
    _env_override: Optional[dict] = None,
    _label: str = "wizard",
    _tenant_id: Optional[int] = None,
    _user_id: Optional[int] = None,
) -> Any:
    """Make a synchronous chat-completion call via the wizard's config
    (or, when `_env_override` is supplied, an arbitrary tool's config —
    see call_repair_llm).

    Returns a parsed dict (when response_format='json') or a string
    (when response_format='text'). Raises LLMClientError on any
    terminal failure; the caller is expected to catch.
    """
    env = _env_override if _env_override is not None else _get_wizard_config()
    base_url = normalize_openai_base_url(env.get("LLM_BASE_URL"))
    model = env.get("LLM_MODEL") or ""
    api_key = env.get("LLM_API_KEY") or ""

    if not base_url:
        raise LLMClientError(f"no LLM base URL configured for {_label} tool")
    if not model:
        raise LLMClientError(f"no LLM model configured for {_label} tool")

    # GY.C7 — labelled-data external-LLM block, shared with the interactive
    # wizard's `_call_llm_impl`. The node-builder path (call_wizard_llm /
    # call_repair_llm) previously bypassed it: it ran the R-042 egress gate in
    # `_post` but never the restricted-label gate. Consult the SAME
    # `wizard_label_block_reason` helper (reads the per-request
    # `_wizard_label_ctx` set by the wizard pipelines with db + tenant_id +
    # referenced file_ids / run_id) and refuse BEFORE any provider call —
    # before the GPU wake and the POST. Local / MOD-hosted targets are never
    # blocked (url_is_external False → no refusal). When a request has no
    # wizard ctx (background / non-wizard callers), the helper is a no-op.
    # 1788g CE carve: routers/support_chat/_shared.py is paid/denied (manifest
    # deny :1304), so the shared helper is fetched via optional_paid. In CE the
    # wizard pipeline that SETS `_wizard_label_ctx` is itself denied, so the
    # helper is a no-op there (ctx unset -> None) — the CE default below is
    # exactly that: allow (None), same return shape.
    from utils.paid_import import optional_paid
    _sc_shared = optional_paid("routers.support_chat._shared")
    _wl_reason = _sc_shared.wizard_label_block_reason(base_url) \
        if _sc_shared is not None else None
    if _wl_reason:
        raise LLMClientError(_wl_reason)

    # OpenAI-compatible endpoint. Ollama, vLLM, OpenRouter, Anthropic-
    # compat proxies, and OpenAI itself all honour this path.
    url = f"{base_url}/v1/chat/completions"

    body: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature if temperature is not None
                       else float(env.get("LLM_TEMPERATURE", "0.3")),
        "max_tokens": max_tokens if max_tokens is not None
                      else int(env.get("LLM_MAX_TOKENS", "4096")),
    }
    if response_format == "json":
        if json_schema:
            # GRAMMAR-CONSTRAINED JSON. `json_object` is only a hint and weak
            # local models (e.g. granite-2b on llama.cpp) IGNORE it — they emit
            # a ```python block and the parse fails at char 0. `json_schema`
            # forces valid structured JSON via the engine's grammar/guided
            # decoding — llama.cpp, vLLM, and OpenAI structured-outputs all
            # honour it — so even a small model produces a parseable object.
            # Falls back to json_object below if the endpoint rejects it.
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": json_schema},
            }
        else:
            body["response_format"] = {"type": "json_object"}

    # Ollama-only knobs (keep_alive, options.num_ctx) are intentionally
    # not sent here any more. vLLM — our managed provider — loads the
    # model once at container start and rejects unknown body fields.
    # Self-hosted Ollama deployments pointing at the `/v1/chat/completions`
    # endpoint don't require them either; the Ollama OpenAI-compat layer
    # reads context from the Modelfile and keeps the model loaded per
    # OLLAMA_KEEP_ALIVE on the server side.

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # llm-stack-wake-idle §1 — record usage BEFORE the call, not after.
    #
    # A long generation that is still streaming is the opposite of idle, and
    # crediting it only on completion would make an actively-used model look
    # untouched for the length of its own longest request. Recording here also
    # means a call that fails still counts: the user asked, and the backend
    # being unreachable is evidence about the backend, not evidence that nobody
    # wants it. Treating a failure as non-usage would let a model that is down,
    # and therefore erroring, be reclaimed for idleness — exactly backwards.
    #
    # Best-effort inside its own try: this feeds a cost optimisation and must
    # never be able to fail a call that would otherwise have succeeded.
    try:
        from services.llm_usage import record_usage
        record_usage(_label, base_url, model)
    except Exception:  # noqa: BLE001
        pass

    # generalized-stack-wake §D8 — wake the served wizard LLM on demand.
    #
    # Before the call reaches the model (`_post` → requests.post, or a dispatch
    # to the model's hosting worker), ask the stack_wake toolset to make the
    # served LLM ready: if it is OFF but fits it is woken; if it cannot fit yet
    # it is held on capacity; if it is already running it is LEFT UP and no
    # wake-ownership is taken. So a wizard request no longer fails merely because
    # the generator was asleep.
    #
    # ADDITIVE + strictly DEFENSIVE, by three separate guarantees:
    #   1. It runs in its OWN short-lived session (this module is otherwise
    #      transport-only and holds no db handle), always closed in `finally`.
    #   2. ensure_llm_ready_for_base_url is itself best-effort (it swallows its
    #      own faults and returns a not-ready result rather than raising), and
    #   3. the whole block is wrapped here so even an import / SessionLocal fault
    #      falls through to TODAY'S EXACT behavior — `_post` still reaches the
    #      model directly or dispatches to its hosting worker.
    # We deliberately do NOT block on readiness for the FITTING / ALREADY-UP
    # cases: `_post` and the worker poll already own the wait, so a still-
    # warming model is handled by the existing timeout / dispatch paths. The ONE
    # exception is `held=True` (D7: the model is OFF and cannot be made to fit —
    # no safe eviction frees a card). That is a capacity WAIT, not an error: the
    # model comes up on its own once capacity frees and the NEXT wizard request
    # (which re-ensures on demand) succeeds — so the request is answered with a
    # 503 "warming / awaiting capacity" instead of failing fast or hanging. A
    # wizard request thus never fails *merely because the model was off* — it
    # either serves, or it waits with an explicit 503.
    try:
        from services import stack_wake
        from database import SessionLocal as _WakeSessionLocal
        _wake_db = _WakeSessionLocal()
        try:
            _wake_res = stack_wake.ensure_llm_ready_for_base_url(
                _wake_db, base_url, tenant_id=_tenant_id, actor=_user_id)
        finally:
            _wake_db.close()
        if getattr(_wake_res, "held", False):
            from fastapi import HTTPException
            logger.info(
                "wizard LLM %s is off and cannot fit right now — holding on "
                "capacity (503); it will serve once capacity frees", base_url)
            raise HTTPException(
                status_code=503,
                detail="The wizard LLM is waking up (model is off and the GPU "
                       "is busy); please try again in a moment.")
    except HTTPException:
        raise  # the held-503 above is a policy answer, not a fault to swallow
    except Exception:  # noqa: BLE001 — a wake attempt must NEVER fail the call
        logger.debug("wizard LLM wake skipped for %s", base_url, exc_info=True)

    try:
        resp = _post(url, body, headers, timeout_s, base_url, api_key)
    except requests.Timeout:
        logger.warning("wizard LLM call timed out after %ss", timeout_s)
        raise LLMClientError(f"timeout after {timeout_s}s")
    except requests.RequestException as e:
        logger.warning("wizard LLM call failed: %s", e)
        raise LLMClientError(f"request failed: {e}")

    if not resp.ok:
        preview = (resp.text or "")[:300]
        # Provider doesn't accept json_schema (some Anthropic-compat proxies /
        # older engines 4xx on it) → fall back to the json_object hint once, so
        # the call still runs where grammar-JSON isn't supported.
        if (json_schema and 400 <= resp.status_code < 500
                and body.get("response_format", {}).get("type") == "json_schema"):
            logger.info("json_schema rejected (HTTP %s) — retrying with "
                        "json_object", resp.status_code)
            body["response_format"] = {"type": "json_object"}
            try:
                resp = _post(url, body, headers, timeout_s, base_url, api_key)
            except (requests.Timeout, requests.RequestException) as e:
                raise LLMClientError(f"request failed: {e}")
        if not resp.ok:
            logger.warning("wizard LLM %s: %s", resp.status_code, preview)
            raise LLMClientError(f"HTTP {resp.status_code}: {preview}")

    try:
        payload = resp.json()
        # Token counts come from the engine's own usage block — vLLM and
        # llama.cpp both return one — never from a character estimate. This is
        # the number a bill would eventually be built on.
        if optional_paid("services.llm_metering") is not None:
            try:
                from services.llm_metering import extract_usage, record_call
                from utils.request_principal import get_principal
                _p = get_principal()
                # mega-qa-hardening Billing #4 — attribute the call to a tenant even
                # when there is no request principal (background / wizard-build /
                # scheduler work). The caller threads the initiating tenant/user via
                # explicit `_tenant_id`/`_user_id`; the request principal wins when
                # present so an in-request call is never mis-attributed.
                #
                # The tenant is resolved here for the CHARGING decision below, but
                # record_call is always called (with whatever we know) so that its
                # own fallback — explicit arg > background-tenant contextvar (set by
                # `llm_tenant_context` at the background entry point) > None — can
                # attribute a background call whose tenant only exists on the job
                # thread's context, not threaded into `_tenant_id`. With nothing set
                # it is a no-op, preserving today's system-work behaviour.
                _meter_tid = getattr(_p, "tenant_id", None) if _p is not None else _tenant_id
                _meter_uid = getattr(_p, "id", None) if _p is not None else _user_id
                _u = extract_usage(payload)
                record_call(
                    tenant_id=_meter_tid,
                    user_id=_meter_uid,
                    tool=_label, model=model, base_url=base_url,
                    prompt_tokens=_u["prompt_tokens"],
                    completion_tokens=_u["completion_tokens"],
                    succeeded=True,
                )
                # §5 — charge on ACTUALS, after the fact, and only if a price
                # exists and charging is on. Separate from the metering above:
                # a charging fault must never cost us the usage row, since the
                # usage data is what a price would eventually be set from.
                # Requires a real request principal (charge_for_call resolves the
                # user's balance) — background calls meter but don't charge here.
                if _p is not None:
                    try:
                        if optional_paid("services.llm_charging") is None:
                            pass  # CE carve: no charging module → no-op, never 500
                        else:
                            from services.llm_charging import charge_for_call
                            from database import SessionLocal as _SL2
                            _cdb = _SL2()
                            try:
                                charge_for_call(
                                    _cdb, user=_p, tool=_label, model=model,
                                    prompt_tokens=_u["prompt_tokens"],
                                    completion_tokens=_u["completion_tokens"])
                            finally:
                                _cdb.close()
                    except Exception:  # noqa: BLE001
                        logger.debug("llm charging: not applied", exc_info=True)
            except Exception:  # noqa: BLE001
                # Metering is bookkeeping. Losing a row is a reporting gap; failing
                # the call would be an outage.
                logger.debug("llm metering: success path", exc_info=True)
    except ValueError:
        raise LLMClientError("LLM returned non-JSON response envelope")

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMClientError(f"unexpected LLM response shape: {str(payload)[:300]}")

    # OpenAI-compatible providers return ``"content": null`` on
    # tool-call-only responses, empty completions, or content-filter
    # trips. Raise a clean LLMClientError so the caller's existing
    # except block routes to its fallback path. Repro 2026-05-31
    # spec=12 attempt 5: a null content here crashed the repair
    # endpoint with TypeError → 500, killing the entire 10-attempt
    # repair budget at a single transient LLM hiccup.
    if content is None:
        finish_reason = ""
        try:
            finish_reason = payload["choices"][0].get("finish_reason") or ""
        except (KeyError, IndexError, TypeError):
            pass
        raise LLMClientError(
            f"LLM returned content=None (finish_reason={finish_reason!r}). "
            f"Likely a transient hiccup, content-filter trip, or "
            f"tool-call-only response — caller should retry or "
            f"surface a conservative fallback."
        )

    if response_format == "text":
        return content

    # response_format == "json" — try to parse, with fence-stripping fallback.
    cleaned = _strip_json_fences(content)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        # Small local models routinely emit PYTHON literals — None/True/False and
        # single quotes — instead of JSON (null/true/false). json.loads rejects
        # them, so EVERY critic verdict was being discarded and the critics failed
        # OPEN, accepting anything (repro 2026-09-03: the intent critic couldn't
        # veto a copies-instead-of-grayscales node because its verdict never
        # parsed). Salvage with ast.literal_eval, which safely evaluates Python
        # literals only (no code execution) — this makes the local-model critics
        # actually function.
        try:
            import ast as _ast
            val = _ast.literal_eval(cleaned)
            if isinstance(val, dict):
                logger.info("wizard LLM: salvaged Python-literal response via literal_eval")
                return val
        except Exception:  # noqa: BLE001 — fall through to the original error
            pass
        logger.warning("wizard LLM returned unparseable JSON: %s | content: %s", e, content[:400])
        raise LLMClientError(f"LLM returned unparseable JSON: {e}")


def _redact_prompt_pair(system_prompt, user_prompt):
    """GY.V4a (GSEC R-086) — PII/PHI redaction at the LLM-bound choke point.

    Applied inside ``call_wizard_llm``, the single entry point every
    node-builder / assistant / wizard / repair call goes through, so the
    provider receives redacted text. Only active when the
    ``privacy.redaction_profile`` platform setting is not 'off'; the profile
    read is cached and any failure returns the prompts unchanged (secret-only
    behaviour), so redaction can never fail a call.
    """
    try:
        from utils import pii_redaction
        if not pii_redaction.profile_active():
            return system_prompt, user_prompt
        return (pii_redaction.redact(system_prompt),
                pii_redaction.redact(user_prompt))
    except Exception:  # noqa: BLE001 — must never fail the caller
        return system_prompt, user_prompt


def call_wizard_llm(*args, **kwargs):
    """Rate-limited entry point. The real work is in _call_wizard_llm_impl.

    A wrapper rather than an inline try/finally because the implementation has
    several return and raise paths, and a slot released on only some of them is
    the leak this limiter exists to avoid — a slot taken and never returned
    permanently shrinks that tenant's ceiling.

    The principal comes from the request context, not a parameter: this
    function's 17 call sites were never given a user to pass. Outside a request
    — APScheduler jobs, boot-time daemons — the context is empty, and the
    limiter treats that as system work and exempts it. That is the intended
    outcome rather than a gap.

    GY.V4a — PII/PHI prompts are redacted here, before the implementation
    builds the messages array, so the provider receives redacted text.
    """
    if len(args) >= 2:
        _sys_p, _usr_p = _redact_prompt_pair(args[0], args[1])
        args = (_sys_p, _usr_p) + tuple(args[2:])
    elif "system_prompt" in kwargs and "user_prompt" in kwargs:
        kwargs["system_prompt"], kwargs["user_prompt"] = _redact_prompt_pair(
            kwargs["system_prompt"], kwargs["user_prompt"])

    slot = None
    try:
        from services import llm_rate_limit as _rl
        from utils.request_principal import get_principal
        principal = get_principal()
        if principal is not None:
            from database import SessionLocal
            db = SessionLocal()
            try:
                slot = _rl.acquire(db, principal, kwargs.get("_label", "wizard"))
            finally:
                db.close()
    except Exception as e:  # noqa: BLE001
        # A refusal is policy and must propagate. Anything else here is
        # bookkeeping, and must never block a call the tenant may make.
        if type(e).__name__ == "RateLimited":
            raise
        logger.debug("llm rate limit: not applied to this call", exc_info=True)
        slot = None

    # §5 — refuse BEFORE the model runs when a tenant plainly cannot pay.
    # Token counts only exist once a call completes, so charging on actuals
    # means the work is already done; raising a payment error there would
    # discard a result the GPU has already produced. A 402 is only honest up
    # front. No-op while charging is off, which is the shipped state.
    try:
        from services import llm_charging as _chg
        from utils.request_principal import get_principal as _gp
        _p = _gp()
        if _p is not None and _chg.charging_enabled():
            from database import SessionLocal as _SL
            _db = _SL()
            try:
                _chg.preflight(_db, _p, kwargs.get("_label", "wizard"))
            finally:
                _db.close()
    except Exception as e:  # noqa: BLE001
        # A payment refusal is policy and must propagate; anything else is
        # bookkeeping and must not block a call the tenant may make.
        if type(e).__name__ == "HTTPException":
            raise
        logger.debug("llm charging: preflight skipped", exc_info=True)

    import time as _time
    _t0 = _time.time()
    try:
        return _call_wizard_llm_impl(*args, **kwargs)
    except Exception:
        # A failed call still consumed input tokens and real GPU time — the
        # prompt was sent. Counting only successes would understate load
        # exactly when the platform is struggling, which is when the number
        # matters most. Token counts are unknown here (there is no payload on a
        # timeout or a connection failure), so the row is honest about that
        # rather than guessing.
        try:
            from services.llm_metering import record_call
            from utils.request_principal import get_principal
            p = get_principal()
            # Billing #4 — same explicit-attribution fallback as the success
            # path: a failed background call still consumed input tokens, and we
            # want it attributed to the initiating tenant, not dropped.
            _fail_tid = getattr(p, "tenant_id", None) if p is not None else kwargs.get("_tenant_id")
            _fail_uid = getattr(p, "id", None) if p is not None else kwargs.get("_user_id")
            # Called unconditionally (record_call is a no-op when its tenant
            # resolves to None) so its own fallback — explicit arg >
            # background-tenant contextvar > None — can attribute a failed
            # background call whose tenant lives only on the job thread's
            # context, not threaded into `_tenant_id`.
            record_call(
                tenant_id=_fail_tid,
                user_id=_fail_uid,
                tool=kwargs.get("_label", "wizard"),
                succeeded=False,
                duration_ms=int((_time.time() - _t0) * 1000),
            )
        except Exception:  # noqa: BLE001
            logger.debug("llm metering: failure path", exc_info=True)
        raise
    finally:
        try:
            from services.llm_rate_limit import release
            release(slot)
        except Exception:  # noqa: BLE001
            # Release failure is survivable: the lease lapses on its own once
            # the heartbeat stops.
            logger.debug("llm rate limit: release failed", exc_info=True)


def call_repair_llm(
    system_prompt: str,
    user_prompt: str,
    *,
    response_format: Literal["json", "text"] = "text",
    timeout_s: int = 180,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    _tenant_id: Optional[int] = None,
    _user_id: Optional[int] = None,
) -> Any:
    """Chat-completion via the REPAIR tool's model (the strong reasoner,
    e.g. gpt-oss-20b), falling back to the wizard model when the repair
    tool isn't configured. Used for the wizard build's repair proposer;
    the fast critic stays on call_wizard_llm by design (cheap reviewer,
    smart proposer).

    `_tenant_id`/`_user_id` thread the initiating tenant for metering when
    called outside a request context (the wizard build authenticates with a
    build-task token, not a user session) — mega-qa-hardening Billing #4.

    ai-config-stack-binding 3.4 — the repair slot is the slot: when it does
    not resolve ``ready`` this raises :class:`LLMClientError` carrying the
    resolver's cause sentence (``no_binding | stack_stopped | stack_missing |
    unreachable``), never a silently substituted wizard model. The caller's
    existing ``except LLMClientError`` renders it (the wizard-build repair
    endpoint reports the rejection reason to the user)."""
    tool, cause = _repair_slot_for(tenant_id=_tenant_id)
    if cause:
        raise LLMClientError(cause)
    return call_wizard_llm(
        system_prompt,
        user_prompt,
        response_format=response_format,
        timeout_s=timeout_s,
        temperature=temperature,
        max_tokens=max_tokens,
        _env_override=_get_tool_config(tool),
        _label=tool,
        _tenant_id=_tenant_id,
        _user_id=_user_id,
    )
