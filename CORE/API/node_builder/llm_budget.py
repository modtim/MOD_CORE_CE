"""Adaptive prompt-size budgets that scale with the underlying LLM's
context window.

The wizard's prompt builders used to carry hardcoded char caps
(5KB per script, 32KB total, 8KB Dockerfile) tuned for a 64K-token
model. When the operator swapped to a 128K- or 256K-context model,
the caps stayed conservative — leaving most of the new window
unused. Worse, if a future model went the other way (small edge
model with 16K context), the caps would silently overflow the
context window and every LLM call would 400.

The fix is to make the model's context window the **single source
of truth** for every prompt-construction cap. This module:

  1. Detects the live LLM's `max_model_len` from the configured
     vLLM endpoint (OpenAI-compatible /v1/models call).
  2. Falls back to an explicit env override (`WIZARD_LLM_CONTEXT_TOKENS`)
     or a conservative default (64000) when detection fails.
  3. Computes a `PromptBudgets` dataclass that downstream prompt
     builders consume — per-script char cap, total-scripts cap,
     Dockerfile cap, output reservation.

Pure-function helpers are unit-testable without any LLM running;
the detection wrapper layers on env override + caching.

Usage:
    from node_builder.llm_budget import default_budgets, compute_budgets

    # Most callers:
    budgets = default_budgets()
    budget_scripts(scripts, budgets=budgets)

    # Explicit override (tests, multi-tenant per-model):
    budgets = compute_budgets(context_tokens=128_000)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from threading import Lock
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# PromptBudgets — pure derivation
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class PromptBudgets:
    """Sizing constants every prompt builder consumes.

    All "*_chars" fields are CHARACTERS, not tokens — prompt builders
    slice strings, not tokenizer outputs. We use 3.5 chars/token as
    the code-and-docs average (English prose averages ~4, pure code
    averages ~3; both end up close to 3.5).

    `context_tokens` is the underlying LLM's max_model_len. Everything
    else derives from it via `compute_budgets()`.
    """
    context_tokens: int
    output_reservation_tokens: int  # LLM response budget
    fixed_overhead_tokens: int      # system prompt + framing + stderr/stdout/history
    chars_per_token: float          # token-to-char conversion estimate
    dockerfile_share: float         # 0..1 of the user-content budget
    scripts_share: float            # 0..1 of the user-content budget
    per_script_share: float         # 0..1 of the scripts budget (cap on any single file)

    @property
    def input_budget_tokens(self) -> int:
        return max(self.context_tokens - self.output_reservation_tokens - self.fixed_overhead_tokens, 0)

    @property
    def input_budget_chars(self) -> int:
        return int(self.input_budget_tokens * self.chars_per_token)

    @property
    def dockerfile_chars(self) -> int:
        return int(self.input_budget_chars * self.dockerfile_share)

    @property
    def total_scripts_chars(self) -> int:
        return int(self.input_budget_chars * self.scripts_share)

    @property
    def per_script_chars(self) -> int:
        return int(self.total_scripts_chars * self.per_script_share)


def compute_budgets(
    context_tokens: int,
    *,
    output_reservation_tokens: int = 4000,
    fixed_overhead_tokens: int = 8000,
    chars_per_token: float = 3.5,
    dockerfile_share: float = 0.20,
    scripts_share: float = 0.80,
    per_script_share: float = 0.125,   # 1/8 — room for 8 files before truncation
) -> PromptBudgets:
    """Pure helper: given a model's context window in tokens, derive
    every char cap the prompt builders need.

    Defaults are tuned conservatively. The shares carve the user-
    content budget into Dockerfile (20%) vs scripts (80%); per_script
    caps any single file at 1/8 of the scripts budget so one huge
    file can't swallow the whole bundle.

    Caller can pass explicit overrides for testing or per-tenant
    tuning. Frozen dataclass output so it's safely hashable.
    """
    if context_tokens <= 0:
        raise ValueError(f"context_tokens must be positive, got {context_tokens}")
    # The shares should sum to ≤ 1 for the user-content budget; not
    # enforced strictly since callers may want slack.
    return PromptBudgets(
        context_tokens=context_tokens,
        output_reservation_tokens=output_reservation_tokens,
        fixed_overhead_tokens=fixed_overhead_tokens,
        chars_per_token=chars_per_token,
        dockerfile_share=dockerfile_share,
        scripts_share=scripts_share,
        per_script_share=per_script_share,
    )


# ---------------------------------------------------------------------
# Context-length detection — auto from vLLM, env override, fallback
# ---------------------------------------------------------------------


_CACHE_LOCK = Lock()
_CACHED_CONTEXT_TOKENS: Optional[int] = None
_DEFAULT_FALLBACK_TOKENS = 64_000


def _context_from_active_preset() -> Optional[int]:
    """Read the active preset's `num_ctx` directly from the AI config.

    For Local-mode deployments (the default), the API itself OWNS the
    max_model_len value — it's declared in LOCAL_PRESETS and surfaced
    in _resolved_effective_env() as `num_ctx`. Using this avoids an
    HTTP round-trip to vLLM and is immune to vLLM-restart races.

    Returns None when num_ctx isn't in the resolved config (Self-hosted
    or Cloud execution mode — those don't declare context length in
    the API config, so we fall through to the /v1/models query).
    """
    try:
        from utils.tool_config import resolved_effective_env
        env = resolved_effective_env("wizard")
        ctx = env.get("num_ctx")
        if isinstance(ctx, int) and ctx > 0:
            return ctx
        # Some code paths stringify ints — accept that too.
        if isinstance(ctx, str) and ctx.isdigit():
            return int(ctx)
    except Exception as e:
        logger.info("llm_budget: preset-config read failed (%s) — will try vLLM", e)
    return None


def _fetch_max_model_len_from_vllm() -> Optional[int]:
    """Query the configured LLM endpoint's /v1/models to learn
    max_model_len. Returns None on any error — the fallback chain
    upstream handles it.

    Works for vLLM out of the box (max_model_len is in the standard
    response). Some other OpenAI-compatible servers don't include it;
    those return None silently and we fall through.
    """
    try:
        # Reuse the wizard's effective LLM config — same source as
        # call_wizard_llm. Avoids duplicating the resolution rules
        # (Vault → env → defaults) here.
        from utils.tool_config import resolved_effective_env
        env = resolved_effective_env("wizard")
        base_url = (env.get("LLM_BASE_URL") or "").rstrip("/")
        api_key = env.get("LLM_API_KEY") or ""
        if not base_url:
            return None
        # GSEC FX.19 R-042 / GY.C8 — the /v1/models probe is an outbound
        # call; skip it for external targets when egress is refused.
        from utils import egress_policy
        if egress_policy.url_is_external(base_url) and \
                egress_policy.refuse_reason(egress_policy.CATEGORY_EXTERNAL_LLM):
            return None
        import httpx
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"{base_url}/v1/models", headers=headers)
        if r.status_code != 200:
            return None
        data = r.json() or {}
        models = data.get("data") or []
        # Find the configured model; if not found, use the first.
        target_model = (env.get("LLM_MODEL") or "").strip()
        chosen = None
        for m in models:
            if isinstance(m, dict) and m.get("id") == target_model:
                chosen = m
                break
        if chosen is None and models:
            chosen = models[0] if isinstance(models[0], dict) else None
        if not isinstance(chosen, dict):
            return None
        # vLLM exposes `max_model_len`; some forks use `context_length`.
        # Be tolerant of both.
        for key in ("max_model_len", "context_length", "max_context_length"):
            v = chosen.get(key)
            if isinstance(v, int) and v > 0:
                return v
        return None
    except Exception as e:
        logger.info("llm_budget: max_model_len detection failed (%s) — will use env/fallback", e)
        return None


def detect_context_tokens() -> int:
    """Return the LLM's context length. Priority:
      1. Explicit env override `WIZARD_LLM_CONTEXT_TOKENS` (operators
         can pin a value when the endpoint lies or doesn't expose it).
      2. Live `/v1/models` query against the configured LLM endpoint.
      3. Conservative fallback of 64000 tokens.

    Detection order:
      1. WIZARD_LLM_CONTEXT_TOKENS env var (operator pin)
      2. Active LOCAL_PRESETS preset's num_ctx (in-memory, no HTTP)
      3. /v1/models query against the configured LLM endpoint
      4. 64K fallback

    Cached per-process. Operators changing presets via the UI also
    trigger invalidate_cache() from the ai_config save endpoint so
    the cache picks up the new value without an API restart.
    """
    global _CACHED_CONTEXT_TOKENS
    with _CACHE_LOCK:
        if _CACHED_CONTEXT_TOKENS is not None:
            return _CACHED_CONTEXT_TOKENS

        # 1. Env override.
        env_val = os.environ.get("WIZARD_LLM_CONTEXT_TOKENS", "").strip()
        if env_val:
            try:
                v = int(env_val)
                if v > 0:
                    _CACHED_CONTEXT_TOKENS = v
                    logger.info("llm_budget: using WIZARD_LLM_CONTEXT_TOKENS=%d from env", v)
                    return v
            except ValueError:
                logger.warning(
                    "llm_budget: WIZARD_LLM_CONTEXT_TOKENS=%r not an int; ignoring",
                    env_val,
                )

        # 2. Active preset (instant, no HTTP).
        from_preset = _context_from_active_preset()
        if from_preset is not None:
            _CACHED_CONTEXT_TOKENS = from_preset
            logger.info("llm_budget: using num_ctx=%d from active preset", from_preset)
            return from_preset

        # 3. Auto-detect from vLLM /v1/models.
        detected = _fetch_max_model_len_from_vllm()
        if detected is not None:
            _CACHED_CONTEXT_TOKENS = detected
            logger.info("llm_budget: auto-detected max_model_len=%d from LLM endpoint", detected)
            return detected

        # 4. Fallback.
        _CACHED_CONTEXT_TOKENS = _DEFAULT_FALLBACK_TOKENS
        logger.warning(
            "llm_budget: could not auto-detect context length; falling back to %d tokens. "
            "Set WIZARD_LLM_CONTEXT_TOKENS to silence this warning.",
            _DEFAULT_FALLBACK_TOKENS,
        )
        return _DEFAULT_FALLBACK_TOKENS


def invalidate_cache() -> None:
    """Public: drop the cached context length so the next detect call
    re-runs the chain. Called by the AI config save endpoint when an
    operator changes presets — without this, the wizard would keep
    using the OLD preset's context size until the API restarts."""
    global _CACHED_CONTEXT_TOKENS
    with _CACHE_LOCK:
        _CACHED_CONTEXT_TOKENS = None
        logger.info("llm_budget: cache invalidated; next prompt build will re-detect")


def reset_cache_for_tests() -> None:
    """Test-only: wipe the cached context length so tests can
    inject different values. NOT for use outside test code."""
    global _CACHED_CONTEXT_TOKENS
    with _CACHE_LOCK:
        _CACHED_CONTEXT_TOKENS = None


def default_budgets() -> PromptBudgets:
    """The one-call helper most prompt builders should use. Resolves
    context length via detect_context_tokens() and computes budgets
    from it."""
    return compute_budgets(detect_context_tokens())
