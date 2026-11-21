"""GT.P2.6a — CE shim for the per-AI-tool config readers.

The paid `routers.ai_config` module owns the LLM-env resolution (`_resolved_effective_env`,
`_read_tool_config`, `is_managed_local_llm`, `critic_env`). open-core/build_public_repo.py
drops that module from the CE tree, so the four CE util call sites
(`utils/llm_client.py`, `utils/embeddings.py`, `utils/image_ref_preflight.py`,
`utils/model_size.py`) used to break at request time on CE.

This shim is the CE-safe entry point:

  * When the paid module is present (Pro), EVERY function delegates to it, so
    Pro behaviour is byte-identical (managed tiers, roles, presets, stack slots).
  * On CE it implements only the Vault/DB read logic: the tool's raw per-tool
    Vault config (`platform/ai-config/<tool>`, legacy fallback) projected into
    the LLM_* env for BYO modes (self_hosted / cloud / advanced) and for
    `provider == "stack"` slots (Persistent Stacks are CE per GT.D5).
    Managed `local` mode is a Pro managed-AI concept (roles/tiers live in the
    denied managed_ai_stacks) — on CE an unconfigured local slot resolves to
    {} ("not configured"), which every caller already handles.

No model catalogue, role binding, preset or prompt logic lives here.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, Optional

from utils.paid_import import optional_paid

logger = logging.getLogger(__name__)

PAID_MODULE = "routers.ai_config"
PSEUDO_TOOLS = ("critic",)


def _paid():
    """The paid ai_config module, or None on CE. Call-time so tests can
    monkeypatch sys.modules."""
    return optional_paid(PAID_MODULE)


# ── CE Vault reads (same paths/semantics as the paid module) ──────────────

def _get_vault_token() -> Optional[str]:
    """Vault token: agent file (paid) else the CE OpenBao AppRole login.

    Without the AppRole fallback every CE read returned {} (the token was
    always None), so per-tool AI configs silently never persisted — see
    FIX-AICONFIG-APPROLE and utils/openbao_client.get_vault_token."""
    from utils.vault_client import get_vault_token
    return get_vault_token()


def _read_vault_path(path: str) -> dict:
    """Read a Vault KV v2 secret. Returns {} if missing."""
    try:
        from utils.vault_client import read_kv_v2
        token = _get_vault_token()
        if not token:
            return {}
        return read_kv_v2(path, token)
    except Exception as e:  # noqa: BLE001
        # FIX-AICONFIG-REVIEW-CHANGES item 4: WARNING, and only the operation +
        # exception CLASS — never the exception text (a VaultError body fragment
        # can carry response content) and never any secret value.
        logger.warning("tool_config Vault read failed for %s: %s", path, type(e).__name__)
        return {}


def _vault_path_for(tool: str) -> str:
    return f"platform/ai-config/{tool}"


def read_tool_config(tool: str) -> dict:
    """Raw Vault config for `tool`, seeded from the legacy shared entry when
    the per-tool path is empty (critic pseudo-tool excepted)."""
    paid = _paid()
    if paid is not None:
        return paid._read_tool_config(tool)

    data = _read_vault_path(_vault_path_for(tool))
    if data:
        return data
    if tool in PSEUDO_TOOLS:
        return {}
    legacy = _read_vault_path("platform/ai-config")
    if legacy:
        seeded = dict(legacy)
        seeded.setdefault("execution_mode", "local")
        return seeded
    return {}


def _model_is_reasoning(model_name: str) -> bool:
    """Free-string heuristic (BYO modes only)."""
    m = (model_name or "").lower()
    return any(t in m for t in ("gpt-oss", "o1", "o3", "o4", "qwq", "-r1",
                                "reason", "thinking"))


def resolved_effective_env(tool: str) -> Dict[str, str]:
    """The tool's config projected into the LLM_* env a task/chat caller needs.

    Pro delegates to the paid resolver (managed roles/presets included). On CE
    this covers BYO modes + `provider == "stack"` (Persistent Stacks are CE);
    managed `local` mode has no CE tier to resolve, so it returns {} — the
    same "not configured" shape every caller already degrades on.
    """
    paid = _paid()
    if paid is not None:
        return paid._resolved_effective_env(tool)

    raw = read_tool_config(tool)
    mode = raw.get("execution_mode", "local")

    if raw.get("provider") == "stack":
        # Persistent Stacks are CE (GT.D5): resolve the running stack's
        # endpoint through the CE service. A stopped/missing stack degrades to
        # {} exactly like the paid resolver (no silent fallback model).
        db = None
        own_session = False
        try:
            from database import SessionLocal
            from services.ai_stack_provider import resolve_stack_endpoint
            db = SessionLocal()
            own_session = True
            base_url, model_name = resolve_stack_endpoint(
                db, raw.get("stack_id"), raw.get("served_model"))
        except Exception as e:  # noqa: BLE001
            logger.warning("tool_config stack provider unresolved tool=%s: %s", tool, e)
            return {}
        finally:
            if own_session and db is not None:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass
        env = {
            "LLM_PROVIDER": "openai_compatible",
            "LLM_MODEL": model_name,
            "LLM_BASE_URL": base_url,
            "LLM_API_KEY": "",
            "LLM_TEMPERATURE": raw.get("temperature", "0.3"),
            "LLM_MAX_TOKENS": raw.get("max_tokens", "4096"),
            "LLM_REASONING": "1" if _model_is_reasoning(model_name) else "",
        }
        return {k: v for k, v in env.items() if v}

    if mode == "local":
        # Managed local serving (tiers/roles/presets) is Pro-only.
        return {}

    # Self-hosted / cloud / advanced: honour the persisted BYO values.
    keep_alive = raw.get("keep_alive") or "forever"
    keep_alive_wire = {"off": "0", "5m": "5m", "30m": "30m", "1h": "1h",
                       "forever": "-1"}.get(keep_alive, "-1")
    env = {
        "LLM_PROVIDER": raw.get("provider") or "",
        "LLM_MODEL": raw.get("model") or "",
        "LLM_BASE_URL": raw.get("base_url") or "",
        "LLM_API_KEY": raw.get("api_key") or "",
        "LLM_TEMPERATURE": raw.get("temperature", "0.3"),
        "LLM_MAX_TOKENS": raw.get("max_tokens", "4096"),
        "LLM_KEEP_ALIVE": keep_alive_wire,
        "LLM_REASONING": "1" if _model_is_reasoning(raw.get("model") or "") else "",
    }
    return {k: v for k, v in env.items() if v}


def is_managed_local_llm(base_url: str) -> bool:
    """True when base_url is one of MOD's managed local-LLM aliases.

    Pro delegates (the alias table lives in the denied managed_ai_stacks).
    CE has no managed AI stacks, so nothing here is ever managed: False.
    """
    paid = _paid()
    if paid is not None:
        return paid.is_managed_local_llm(base_url)
    return False


def critic_env() -> Dict[str, str]:
    """Effective LLM env for the wizard's review pass, honouring
    `critic_mode` ('self' → wizard env, else the critic pseudo-tool's env).
    Pro delegates; CE runs the same read logic. Never raises."""
    paid = _paid()
    if paid is not None:
        return paid.critic_env()
    try:
        mode = (read_tool_config("wizard") or {}).get("critic_mode")
        if mode == "self":
            return resolved_effective_env("wizard")
        return resolved_effective_env("critic")
    except Exception:  # noqa: BLE001
        try:
            return resolved_effective_env("wizard")
        except Exception:  # noqa: BLE001
            return {}


# ── GT.P2.6d — managed-stack side-channel helpers (E40/E41/E44) ────────────
#
# These read managed-AI state that has NO CE home: role→endpoint binding
# (`managed_ai_stacks`, denied) and the served-model table (`llm_idle`,
# denied). On CE every one returns the "inert" shape the callers already
# degrade on (None / ""), so a managed stack can neither be addressed nor
# answer for a tool. Pro delegates to routers.ai_config byte-for-byte.

def role_base_url(role: str) -> Optional[str]:
    """Where a managed role's model answers from here (Pro). CE: None —
    managed roles don't exist without the denied managed_ai_stacks."""
    paid = _paid()
    if paid is not None:
        return paid._role_base_url(role)
    return None


def managed_key(role: Optional[str] = None) -> str:
    """Bearer token guarding the managed model servers (Pro). CE: "" —
    nothing managed to guard."""
    paid = _paid()
    if paid is not None:
        return paid._managed_key(role)
    return ""


def stack_provider_error(tool: str) -> Optional[str]:
    """Why a `provider == "stack"` slot's `resolved_effective_env` came back
    empty — a side-channel for callers that already found "nothing
    configured", to tell a human WHY (stopped/missing stack) before falling
    back to the generic not-configured message.

    Pro delegates to the paid diagnostic. CE: the stack-provider service is
    allowed but the managed-stack state it explains has no CE home, so there
    is nothing to report — None. Never raises."""
    paid = _paid()
    if paid is not None:
        return paid._stack_provider_error(tool)
    return None


def stack_custom_base_url(st, db) -> Optional[str]:
    """A reachable base URL for a NON-managed Persistent Stack: mesh
    MagicDNS name of the serving service, else the legacy published port,
    else the hosting worker's advertised address.

    Single copy (2400GX-ceLLM): services.stack_model_endpoint is CE-allowed,
    so BOTH editions call it directly — no `_paid()` branch, no None on CE."""
    from services.stack_model_endpoint import custom_base_url
    return custom_base_url(st, db)
