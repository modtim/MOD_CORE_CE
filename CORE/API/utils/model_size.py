"""node-role / node-build §scale-with-model — resolve the VRAM footprint of the
model a tool would use, so limits/timeouts can scale with model size instead of
being hardcoded. Small model → looser (needs retries + longer generation); big
model → tighter (fast, expensive). Buy speed with bigger GPUs or a cloud model.

Kept dependency-light and in `utils/` so both the node router (rate limit) and
the node_builder synth path (timeout) can import it without a circular import.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


def resolved_model_vram_gb(tool: str = "wizard") -> float:
    """VRAM floor (GB) of the model `tool` resolves to. Source order:
    the serving managed-AI stack's accelerator floor (accurate), then the
    parameter count parsed from the model name (rough Q4 estimate), then a
    medium default. Never raises."""
    env = {}
    try:
        from utils.tool_config import resolved_effective_env
        env = resolved_effective_env(tool) or {}
    except Exception:  # noqa: BLE001
        pass
    model = (env.get("LLM_MODEL") or "").lower()
    base = (env.get("LLM_BASE_URL") or "").lower()

    # 1) the serving managed-AI stack's accelerator floor.
    from utils.paid_import import optional_paid
    _mai = optional_paid("services.managed_ai_stacks")
    if _mai is None:
        # CE carve: no managed-AI stacks exist, so there is no accelerator floor
        # to read — fall through to the parameter-count estimate below.
        logger.debug("managed_ai_stacks not in this edition for %s", tool)
    else:
        try:
            import models
            from database import SessionLocal
            GENERATOR_STACK_NAME = _mai.GENERATOR_STACK_NAME
            REASONER_STACK_NAME = _mai.REASONER_STACK_NAME
            db = SessionLocal()
            try:
                for s in (db.query(models.PersistentStack)
                          .filter(models.PersistentStack.name.in_(
                              [GENERATOR_STACK_NAME, REASONER_STACK_NAME])).all()):
                    svc = (s.compose_spec or {}).get("services", {}).get("llm", {})
                    xmod = svc.get("x-mod") or {}
                    served = str((xmod.get("served") or {}).get("model") or "").lower()
                    vram = (xmod.get("accelerator") or {}).get("min_vram_gb")
                    alias_hit = ((s.name == GENERATOR_STACK_NAME and "ollama" in base)
                                 or (s.name == REASONER_STACK_NAME
                                     and "vllm-big" in base))
                    if vram and (served == model or (model and model in served)
                                 or alias_hit):
                        return float(vram)
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            logger.debug("vram lookup via stack failed for %s", tool, exc_info=True)

    # 2) parameter count in the model name → rough Q4 VRAM.
    m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", model)
    if m:
        return max(2.0, float(m.group(1)) * 0.7 + 1.5)

    # 3) medium default.
    return 10.0


def scaled_by_inverse_vram(base_gb_units: float, floor: int, ceil: int,
                           tool: str = "wizard",
                           vram_gb: Optional[float] = None) -> int:
    """`clamp(base_gb_units / vram, floor, ceil)`. A small model (low VRAM)
    gets a bigger value; a big model gets a smaller one. Used for both the
    per-window call budget and the generation timeout."""
    try:
        vram = max(1.0, vram_gb if vram_gb is not None
                   else resolved_model_vram_gb(tool))
        return int(max(floor, min(ceil, base_gb_units / vram)))
    except Exception:  # noqa: BLE001
        return floor
