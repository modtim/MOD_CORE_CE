"""
AI Configuration Endpoint — per-tool LLM settings stored in Vault.

Three tools have independent configs, each written at a separate Vault path
under secret/data/platform/ai-config/{tool} where tool ∈ {wizard, adapter,
assistant}:
  - wizard     → Node Wizard Generator (/node_definitions/generate-artifacts)
  - adapter    → ContainerAdapter (/node_definitions/from-container + /generate-dockerfile)
  - assistant  → AI Assistant (chat)

Each config exposes an `execution_mode` that picks from four presets:
  - "local"       → MOD's compose-level Ollama (http://ollama:11434) — recommended
  - "self_hosted" → user-operated Ollama (admin supplies base_url + optional model)
  - "cloud"       → external provider (OpenAI/Anthropic/etc.)
  - "advanced"    → every knob exposed

Local mode points at the `ollama` service in docker-compose (one shared
Ollama with devstral:latest loaded on a dedicated GPU). Requires an NVIDIA
GPU on the compose host; tenants without one configure Cloud API or
Self-hosted Ollama instead. The compose service is treated as infrastructure
(same class as vault/qdrant/postgres) — NOT as a MOD NodeDefinition. The
general Service Nodes abstraction for future stateful workloads is still
parked in FUTURE_STATEFUL_CONTAINERS_PLAN.md.

Legacy compatibility: GET/PUT /platform/ai-config (without /{tool}) still
exist as deprecated shims that read/write the wizard tool's config.
"""

import logging
import os
import urllib.parse
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from dependencies import get_db, require_active_and_onboarded_user

import models

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/platform",
    tags=["Platform Settings"],
)


# Tier-managed tools. These drive tier_capabilities(), the managed-Ollama
# residency reconciler, the advanced-config stash/restore, and get_all — every
# place that reasons about "what the active hardware tier serves for each tool".
TOOLS = ("wizard", "adapter", "assistant", "support", "repair")

# separate-reasoner-model §1 — "critic" is a PSEUDO-tool, not a tier-managed one.
# It is the Node Wizard's REVIEW side (critique_script / critique_executability /
# critique_directions + the coder-stall escalation), and it is persistable and
# resolvable exactly like a real tool — `_resolved_effective_env("critic")` has
# always read the Vault path `platform/ai-config/critic`. What it must NOT do is
# join TOOLS: it has no tier row of its own (it borrows the tier's reasoner role
# when left managed), so it must not appear in tier_capabilities / unavailable_
# tools / residency. CONFIGURABLE_TOOLS is therefore TOOLS + the pseudo-tools
# whose config the GET/PUT/all endpoints accept and persist.
PSEUDO_TOOLS = ("critic",)
CONFIGURABLE_TOOLS = TOOLS + PSEUDO_TOOLS
ToolName = Literal["wizard", "adapter", "assistant", "support", "repair", "critic"]
ExecutionMode = Literal["local", "self_hosted", "cloud", "advanced"]

# Local-mode presets. All three tools share the one compose-level vLLM
# server at http://ollama:11434 (the "ollama" host-alias is preserved
# during the Ollama→vLLM cutover for backward-compatible env plumbing).
# Each preset maps to a vLLM launch config in docker-compose.yml.
#
# Unlike Ollama, vLLM is a single-model-per-container server — changing
# preset means restarting the compose service (docker compose up -d
# --force-recreate vllm). The API no longer does hot swapping; preset
# selection is a deployment-level decision.
#
# Provenance guardrail: MOD policy excludes China/Russia-origin models.
# Allied-country weights are fine (Mistral AI, France and Meta, US
# are explicit here). The `vendor` field is surfaced in the Settings UI
# so it's auditable at a glance.
#
# The `served_name` must match vLLM's --served-model-name flag for each
# preset — the API sends this as the `model` field to /v1/chat/completions.
# per-rung-reasoner §1.1 — the reasoner belongs to the rung.
#
# It was a single hardcoded model (gpt-oss-20b, 22.5 GB) that `definitions()`
# appended to every vLLM rung, so its requirements were independent of the
# hardware the rung was chosen for. That is how a rung named "Compact (16 GB
# GPU)" came to demand 37.5 GB.
#
# `reasoner: None` means THIS RUNG PROVIDES NO REASONING, which is a real
# answer on small hardware and must be distinguishable from a reasoner that
# failed to provision — those need opposite responses from an operator.
#
# Kept in the same row as the generator rather than in a parallel table keyed by
# the same names. Two tables keyed identically drift, and drift between a rung
# and its reasoner is the exact bug being fixed here.
REASONER_KEY = "reasoner"

LOCAL_PRESETS: Dict[str, Dict[str, Any]] = {
    # --- rungs below Compact -------------------------------------------------
    #
    # Until these existed the ladder bottomed out at Compact's 16 GB floor and
    # `auto_select_llm_preset` returned "none" below it — an 8 GB box was told
    # to go use a cloud API. The capability was already here (the ai-coder-
    # granite stack serves granite-3.3-2b at 0.36 GPU utilisation, ~5.8 GB),
    # it was simply never wired into the ladder the installer reads.
    #
    # CONTEXT, NOT VRAM, IS THE BINDING CONSTRAINT. `llm_budget.compute_budgets`
    # reserves 4000 output + 8000 fixed overhead before any user content, so a
    # 4K or 8K context leaves the Node Wizard exactly ZERO input budget — it
    # would not error, `input_budget_tokens` is clamped at 0, it would just
    # silently have nothing to work with. 16K is therefore the lowest context
    # at which the wizard functions at all (4,384 tokens of headroom), and both
    # of these tiers sit there rather than at the 4K the granite stack used.
    "minimal": {
        # §1.4 — one accelerator. A single generator, nothing to split.
        "cards": 1,
        # §1.1 — no reasoning on this rung. Declared, not omitted: this rung
        # never reached the reasoner branch in `definitions()`, so the absence
        # was a side effect of how it is served rather than a decision. Now it
        # is the decision.
        "reasoner": None,
        "label": "Minimal (5 GB GPU) — NVIDIA / AMD / Intel, text only",
        # Card floor, NOT the model footprint — see vram_est_gb.
        "vram_floor_gb": 5,
        "served_name": "granite3.3:2b",
        "hf_repo": "ibm-granite/granite-3.3-2b-instruct",
        # Q4 GGUF through Ollama, NOT BF16 through vLLM, and not AWQ either.
        # Marlin is CUDA-only, so an AWQ low rung would be NVIDIA-only — and
        # the low rung is precisely where vendor breadth matters, since anyone
        # with serious throughput needs buys a pro card and lands on compact
        # or above. GGUF runs on Ollama's CUDA and ROCm builds from one config.
        "quantization": "Q4_K_M (GGUF)",
        "vendor": "IBM (US)",
        # llama.cpp, not Ollama. Same GGUF, same engine underneath, but
        # measured leaner and faster: 3.04 GiB peak vs 4.46, and a ~15k-token
        # prompt in 20.2s vs 103s. Upstream also ships a SYCL build, which is
        # what makes Intel a first-class vendor rather than a special case.
        "serving_stack": "llama.cpp",
        "gguf_repo": "ibm-granite/granite-3.3-2b-instruct-GGUF:Q4_K_M",
        # granite-3.3-2b is TEXT-ONLY. Every larger preset is Devstral with
        # vision, so this rung genuinely changes what the product can do, not
        # just how fast it does it. tier_capabilities() is what makes that
        # visible instead of surprising.
        "vision": False,
        "max_model_len": 16384,
        # MEASURED on an RTX 5000 Ada, granite Q4 on llama.cpp at 16K:
        # 3.03 GiB resident, 3.04 GiB peak under a ~15k-token prompt.
        "vram_est_gb": 4,
        # One sequence. Each concurrent slot needs its own KV cache, and on a
        # card this size a second slot is the difference between starting and
        # not.
        "concurrent_users": 1,
    },
    "cpu": {
        # §1.4 — no accelerator at all.
        "cards": 0,
        # §1.1 — no reasoning on this rung. Declared, not omitted: this rung
        # never reached the reasoner branch in `definitions()`, so the absence
        # was a side effect of how it is served rather than a decision. Now it
        # is the decision.
        "reasoner": None,
        "label": "CPU only (no GPU) — chat and evaluation",
        # Card floor, NOT the model footprint — see vram_est_gb.
        "vram_floor_gb": 0,
        "served_name": "granite-3.3-2b",
        "hf_repo": "ibm-granite/granite-3.3-2b-instruct",
        "quantization": "Q4_K_M (GGUF)",
        "vendor": "IBM (US)",
        "vision": False,
        # 8K, NOT 16K, and the shortfall is deliberate.
        #
        # Measured on 4 cores: a chat-shaped turn takes 17-23s, while a
        # wizard-shaped call with ~4.4k tokens of input did not complete in 20
        # minutes. The large-context tools are not usable here at any context
        # length, so serving a window big enough to accept their prompts only
        # buys the KV cache that OOM-killed an 8 GiB box.
        #
        # At 8K the 12,000-token prompt overhead exceeds the window, so
        # tier_capabilities() gates wizard/adapter/repair OFF automatically —
        # the same arithmetic that made 16K the floor for the GPU rungs now
        # states the honest answer for CPU. Anyone who wants those tools needs
        # a GPU, which is what the `minimal` rung is for.
        #
        # Reducing context is a MEMORY fix, not a speed one: prefill cost
        # tracks the tokens actually sent, not the configured ceiling.
        "max_model_len": 8192,
        # RAM, not VRAM. Kept as vram_est_gb so every consumer of this table
        # keeps working; compute_target below is what says "there is no card".
        "vram_est_gb": 0,
        "concurrent_users": 1,
        "compute_target": "cpu",
        # Measured, then reduced with the window. granite3.3:2b at 16K held
        # ~8.1 GiB RSS and an 8 GiB container was OOM-killed mid-request. At
        # 8K with a single parallel slot the KV cache is a quarter of that.
        "min_ram_gb": 8,
        # llama.cpp, not vLLM: it serves an OpenAI-compatible /v1 and runs
        # properly on CPU. Same engine as the GPU rung, CPU build — one config
        # shape across both, so nothing downstream changes.
        "serving_stack": "llama.cpp",
        "gguf_repo": "ibm-granite/granite-3.3-2b-instruct-GGUF:Q4_K_M",
    },
    "compact": {
        # §1.4 — one accelerator, and the rung is named for it. Its stacks must
        # therefore BOTH fit that one card, which is what the integrity check
        # enforces and what the old 22.5 GB reasoner violated.
        "cards": 1,
        # §4.1 — MEASURED PAIR. granite-3.3-8b generates, Nemotron-Nano reasons,
        # both resident on one card.
        #
        # MEASURED on an RTX 5000 Ada (16376 MiB), Q4_K_M on llama.cpp at a 16K
        # window, one slot, peak under a ~12k-token prompt:
        #     granite-3.3-8b   9185 MiB total  ->  8.05 GiB attributable
        #     Phi-4-mini-reas  6287 MiB total  ->  4.66 GiB attributable
        #     BOTH RESIDENT   13958 MiB total  -> 12.72 GiB attributable
        # The pair measured together equals the sum of the solo measurements
        # exactly, so co-tenancy costs nothing extra here.
        #
        # 2026-08-10: the reasoner became Nemotron-Nano-8B (see below).
        # Re-measured as a live pair on the same card, both stacks deployed:
        #     BOTH RESIDENT   14590 MiB idle -> 14.25 GiB
        #     after a 14,038-token prompt    -> 14598 MiB (+8 MiB)
        # llama.cpp preallocates the full --ctx-size KV cache at load, so idle
        # IS peak and the two numbers above are the same measurement. That is
        # ~1.5 GiB more than the Phi pair, and ~1.7 GiB of card still spare.
        #
        # A WARNING FOR ANY FIGURE TAKEN ON THIS BOX: Windows energy saver caps
        # this GPU at 35 W against an 80 W default, which pins clocks to 210 MHz
        # under load and made local inference 9x slower (11 -> 107 tok/s once
        # lifted). VRAM figures are unaffected; every THROUGHPUT number is not.
        #
        # Both models are allied-origin (IBM US, Microsoft US). MOD policy
        # excludes China/Russia-origin weights, which is why the obvious
        # small-reasoner candidates are not here.
        #
        # WHY GGUF AND NOT AWQ. AWQ-Marlin is CUDA-only, so an AWQ pair would
        # make this rung NVIDIA-only while appearing to broaden the ladder.
        # llama-server takes identical flags on every backend, so this reaches
        # NVIDIA, AMD and Intel by changing only the image tag — and only CUDA
        # has actually been run here, which every surface says.
        # REPLACED Phi-4-mini-reasoning 2026-08-10. Phi failed this role on two
        # counts, both measured on this box:
        #   1. It never called a tool. Given `tools=[lookup_site_map]` it
        #      returned `tool_calls: None` and asked the user what software
        #      they were using. It is a MATH reasoner — it calls its input "a
        #      problem", answers in \boxed{}, and ignores the format contract.
        #      No tool call means no site map, so no <ACTIONS> block, so the
        #      assistant could never navigate anywhere.
        #   2. Its "reasoning" was not usable reasoning here.
        # Nemotron-Nano answers the same prompts correctly, and — because it is
        # Llama-3.1-based — llama-server HAS a parser for its tool-call format
        # and returns a real `tool_calls` array. Granite's format has no parser
        # in llama.cpp, which is the same trap from the other direction.
        # Thinking is toggled by a system message: "detailed thinking on|off".
        "reasoner": {
            "served_name": "llama-3.1-nemotron-nano-8b",
            "hf_repo": "nvidia/Llama-3.1-Nemotron-Nano-8B-v1",
            "gguf_repo": "bartowski/nvidia_Llama-3.1-Nemotron-Nano-8B-v1-GGUF:Q4_K_M",
            "quantization": "Q4_K_M (GGUF)",
            "serving_stack": "llama.cpp",
            "vendor": "NVIDIA (US)",
            # 32K, doubled from 16K on 2026-08-10 and paid for by quantising
            # the KV cache rather than by taking headroom (see gguf_compose).
            # MEASURED: KV is 2048 MiB at f16/16K and 1088 MiB at q8_0/16K, so
            # 32K quantised costs LESS than 16K did unquantised.
            #
            # Specified against a quantised cache. A backend without one halves
            # this at deploy so the footprint — which every floor below depends
            # on — stays identical; only cuda is gguf_verified today.
            #
            # 2026-09-04: cut 32768 -> 16384. On a 16 GB card the granite+nemotron
            # PAIR fits at ~15.4 GiB by design, leaving only ~0.36 GiB free — too
            # little to absorb concurrent-inference compute-buffer spikes when the
            # coder and critic run at once, which degraded the critic intermittently.
            # The wizard needs at most WIZARD_MIN_CONTEXT_TOKENS (16384), so 32K was
            # pure overhead here; halving KV (nemotron 1.06 -> 0.53 GiB) buys the
            # headroom back. floors below are now conservative (assume the old 32K).
            "max_model_len": 16384,
            # 5.68 GiB measured -> 6.5 declared, the same ~0.8 GiB of pessimism
            # the other rows carry (granite 8.05 -> 9, the old Phi 4.66 -> 5.5).
            #
            # The load-time delta IS the peak here, confirmed rather than
            # assumed: a 14,038-token prompt moved the card by 8 MiB. llama.cpp
            # preallocates the whole --ctx-size KV cache at load, so there is no
            # later growth to leave headroom for. An earlier revision of this
            # entry declared 6.8 and warned the figure was not comparable to the
            # rows above — that caution was unnecessary, and 15.8 GB of summed
            # floors made a 14.25 GiB pair look like it barely fitted.
            # 5.81 GiB computed at 32K/q8_0 from measured parts (3.69 GiB of
            # weights + 1.06 GiB of quantised KV) -> 6.2 declared. Padding is
            # thinner than the older rows carry because llama.cpp preallocates
            # the whole KV cache at load: a 14,038-token prompt moved the card
            # by 8 MiB, so load either succeeds outright or fails outright, and
            # there is no later growth to reserve against.
            "vram_floor_gb": 6.2,
            "vram_est_gb": 5.8,
            "max_num_seqs": 1,
            "vision": False,
        },
        "label": "Compact (16 GB GPU) — single tenant",
        # Card floor, NOT the model footprint — see vram_est_gb.
        "vram_floor_gb": 16,
        "served_name": "granite-3.3-8b",
        "hf_repo": "ibm-granite/granite-3.3-8b-instruct",
        "gguf_repo": "ibm-granite/granite-3.3-8b-instruct-GGUF:Q4_K_M",
        "quantization": "Q4_K_M (GGUF)",
        "vendor": "IBM (US)",
        # llama.cpp, not vLLM — this is what makes the rung multi-vendor. See
        # the reasoner note above.
        "serving_stack": "llama.cpp",
        # TEXT ONLY. Devstral had vision and this does not, so a deployment
        # moving to this rung LOSES image input. tier_capabilities() surfaces it
        # before selection, which is the whole reason that field exists.
        "vision": False,
        # 32K, same doubling and same reasoning as the reasoner above. Granite
        # carries MORE KV per token than Nemotron — 2560 MiB against 2048 at
        # f16/16K, measured — so it gains the most from quantising it.
        # 2026-09-04: cut 32768 -> 16384 (see reasoner note). Granite gains the
        # most: KV 1.33 -> 0.66 GiB. Pair now fits with ~1.5 GiB free instead of
        # ~0.36, absorbing concurrent coder+critic compute spikes. 16384 ==
        # WIZARD_MIN_CONTEXT_TOKENS, the wizard's floor, so no capability is lost.
        "max_model_len": 16384,
        # 8.72 GiB computed at 32K/q8_0 from measured parts (6.06 GiB of
        # weights + 1.33 GiB of quantised KV) -> 9.2 declared. This is the
        # GENERATOR's floor, not the rung's card floor: 9.2 + 6.2 = 15.4
        # against a 16 GB card, which is what the ladder-integrity check
        # verifies. The pair measures ~14.5 GiB resident.
        "generator_vram_floor_gb": 9.2,
        "vram_est_gb": 8.7,
        # One slot each. Two servers already share this card; a second slot on
        # either is the difference between starting and not.
        "concurrent_users": 1,
    },
    "balanced": {
        # §1.4 — TWO accelerators: generator on one, reasoner on the other.
        # Previously expressed only as `device_ids: ["1"]` on the vllm-big
        # service in docker-compose — a placement intent no surface read and no
        # check consulted, which is why a whole-ladder check first read this
        # rung as broken.
        "cards": 2,
        # §1.1 — what this rung resolves to TODAY, recorded verbatim so this
        # step changes no behaviour. The 22.5 floor is honest for the model:
        # gpt-oss-20b is ~16 GB of MXFP4 weights and wants room for KV cache at
        # 128K. It is simply far too large for the card this rung names, which
        # is what the integrity check in §1.4 exists to catch.
        "reasoner": {
            "served_name": "gpt-oss-20b",
            "hf_repo": "openai/gpt-oss-20b",
            "quantization": None,          # MXFP4, vLLM auto-detects
            "serving_stack": "vllm",
            "max_model_len": 131072,
            "vram_floor_gb": 22.5,
            "max_num_seqs": 16,
            # multi-vendor §3 — the non-CUDA serving path. Same MODEL, different
            # engine: llama.cpp serves GGUF on CUDA, ROCm, Intel and Vulkan from
            # one config, so a rung stops being CUDA-locked by its kernel choice.
            # ggml-org is llama.cpp's own org, and gpt-oss ships MXFP4 natively
            # so the GGUF is MXFP4 rather than a K-quant.
            # Verified present 2026-08-05: gpt-oss-20b-MXFP4.gguf, 12.1 GB.
            "gguf_repo": "ggml-org/gpt-oss-20b-GGUF:MXFP4",
            # DERIVED, not measured: 12.1 GB of weights against the headroom
            # ratio compact's MEASURED floors use (granite 8B Q4 ≈ 4.9 GB → 9.0).
            # Marked unverified until it runs on real hardware.
            "gguf_vram_floor_gb": 20.0,
        },
        "label": "Balanced (24 GB GPU) — small team — recommended",
        # Card floor, NOT the model footprint — see vram_est_gb.
        "vram_floor_gb": 24,
        "served_name": "devstral-small-2",
        "hf_repo": "cyankiwi/Devstral-Small-2-24B-Instruct-2512-AWQ-4bit",
        "quantization": "AWQ INT4",
        # multi-vendor §3 — the non-CUDA serving path for this rung's
        # GENERATOR. awq_marlin is CUDA-only, which is what locked this rung to
        # NVIDIA; llama.cpp serves the same model from GGUF on every vendor.
        # lmstudio-community is already this ladder's GGUF source for compact's
        # Phi-4, so the provenance is consistent.
        # Verified present 2026-08-05:
        # Devstral-Small-2-24B-Instruct-2512-Q4_K_M.gguf, 14.3 GB.
        "gguf_repo": "lmstudio-community/Devstral-Small-2-24B-Instruct-2512-GGUF:Q4_K_M",
        # DERIVED from 14.3 GB of weights using the headroom ratio compact's
        # MEASURED floors use. Unverified until it runs on real hardware.
        "generator_gguf_vram_floor_gb": 22.0,

        "vendor": "Mistral AI (FR)",
        "vision": True,
        "max_model_len": 32768,
        "vram_est_gb": 22,
        "concurrent_users": 48,
    },
    "production": {
        # §1.4 — TWO accelerators, as `balanced`. See the note there.
        "cards": 2,
        # §1.1 — what this rung resolves to TODAY, recorded verbatim so this
        # step changes no behaviour. The 22.5 floor is honest for the model:
        # gpt-oss-20b is ~16 GB of MXFP4 weights and wants room for KV cache at
        # 128K. It is simply far too large for the card this rung names, which
        # is what the integrity check in §1.4 exists to catch.
        "reasoner": {
            "served_name": "gpt-oss-20b",
            "hf_repo": "openai/gpt-oss-20b",
            "quantization": None,          # MXFP4, vLLM auto-detects
            "serving_stack": "vllm",
            "max_model_len": 131072,
            "vram_floor_gb": 22.5,
            # multi-vendor §3 — same model, same non-CUDA path as balanced.
            "gguf_repo": "ggml-org/gpt-oss-20b-GGUF:MXFP4",
            "gguf_vram_floor_gb": 20.0,
            "max_num_seqs": 16,
        },
        "label": "Production (48 GB+ GPU) — SaaS / hundreds of users",
        # Card floor, NOT the model footprint — see vram_est_gb.
        "vram_floor_gb": 48,
        "served_name": "devstral-small-2",
        "hf_repo": "mistralai/Devstral-Small-2-24B-Instruct-2512",
        # multi-vendor §3 — the non-CUDA serving path for this rung's
        # GENERATOR. awq_marlin is CUDA-only, which is what locked this rung to
        # NVIDIA; llama.cpp serves the same model from GGUF on every vendor.
        # lmstudio-community is already this ladder's GGUF source for compact's
        # Phi-4, so the provenance is consistent.
        # Verified present 2026-08-05:
        # Devstral-Small-2-24B-Instruct-2512-Q4_K_M.gguf, 14.3 GB.
        "gguf_repo": "lmstudio-community/Devstral-Small-2-24B-Instruct-2512-GGUF:Q4_K_M",
        # DERIVED from 14.3 GB of weights using the headroom ratio compact's
        # MEASURED floors use. Unverified until it runs on real hardware.
        "generator_gguf_vram_floor_gb": 22.0,

        "quantization": "FP8 (native)",
        "vendor": "Mistral AI (FR)",
        "vision": True,
        # 128K context = Mistral's recommended deployment for Devstral 2
        # on 48 GB at FP8. With kv-cache-dtype=fp8 (set in docker-compose
        # for the production preset), KV cache fits ~2 concurrent slots
        # at 128K, leaving headroom for prefix caching + batching.
        # Stepped up from 64K once Devstral-Small-2 24B FP8 shipped with
        # native 128K context and the wizard's adaptive prompt budgets
        # were wired to track this value through the AI config layer.
        "max_model_len": 131072,
        "vram_est_gb": 44,
        "concurrent_users": 128,
    },
}
PresetName = Literal["cpu", "minimal", "compact", "balanced", "production"]

# Ascending capability. Used by the installer's auto-select and by anything
# that needs to reason about "the next rung down".
PRESET_LADDER = ("cpu", "minimal", "compact", "balanced", "production")

# The context floor below which the Node Wizard has no input budget at all.
# Derived, not guessed: llm_budget.compute_budgets reserves
# output_reservation_tokens (4000) + fixed_overhead_tokens (8000) = 12000
# before any user content, and input_budget_tokens is max(ctx - 12000, 0).
# At 8192 that is 0; at 16384 it is 4384.
WIZARD_MIN_CONTEXT_TOKENS = 16384

# ---------------------------------------------------------------------------
# managed-ai-self-deploy §1 — the tier as a SETTING, not a build parameter.
# ---------------------------------------------------------------------------
#
# `DEFAULT_PRESET` used to be a module-level `os.getenv("MANAGED_OLLAMA_PRESET",
# "balanced")`, read exactly once at import time. Writing a new value to Vault
# could never take effect: the process had already frozen the answer into a
# constant before any operator could reach a settings page. This replaces it
# with a resolver that is consulted at CALL time, so a stored setting actually
# governs the next request — the whole point of exposing it as a setting.
#
# Resolution order, per field: stored (Vault) -> env -> hardcoded default. The
# source that won is reported alongside the value so the surface that lets an
# operator change this can also show WHY the current value is what it is,
# rather than a bare "balanced" with no way to tell a deliberate choice from an
# untouched default.
_MANAGED_AI_SETTING_PATH = "platform/ai-config/_managed_ai_setting"

# A cheap in-process cache so a request path that consults the resolver several
# times (tier_capabilities -> _rung_requirement -> ... across five presets, for
# instance) does not turn into five-plus Vault round trips. Short TTL: long
# enough to collapse one request's fan-out, short enough that a change made
# through the write endpoint is visible well within a human's next click even
# without the explicit invalidation the write endpoint also does.
_SETTING_CACHE_TTL_S = 5.0
_managed_ai_setting_cache: Dict[str, Any] = {"ts": 0.0, "value": None}


class ManagedAISetting(BaseModel):
    """What an operator may persist. Both optional — PUT-ing one field leaves
    the other untouched, matching the rest of this file's partial-update
    convention (see ToolAIConfigUpdate)."""
    tier: Optional[str] = None
    accelerator_kind: Optional[str] = None


class ManagedAISettingResolved(BaseModel):
    """The resolver's answer: a value for each field, PLUS which source won.

    §1.2 — the surface that lets an operator change this needs to say whether
    "balanced" is what they chose, what the environment says, or simply the
    hardcoded fallback nobody has touched. Those are different facts and only
    one of them means "someone already decided this".
    """
    tier: str
    tier_source: Literal["stored", "env", "default"]
    accelerator_kind: str
    accelerator_kind_source: Literal["stored", "env", "default"]
    # §1.5 — an unrecognised stored value falls back rather than raising, but
    # is NAMED here so the settings page that would let someone fix the typo
    # can say what was wrong instead of silently overriding it.
    invalid_stored_tier: Optional[str] = None
    invalid_stored_accelerator_kind: Optional[str] = None


def _default_accelerator_kind_fallback() -> str:
    try:
        from services.managed_ai_stacks import DEFAULT_ACCELERATOR_KIND
        return DEFAULT_ACCELERATOR_KIND
    except Exception:  # noqa: BLE001 — must never take out the resolver
        return "cuda"


def _resolve_managed_ai_setting_uncached() -> "ManagedAISettingResolved":
    stored = _read_vault_path(_MANAGED_AI_SETTING_PATH) or {}

    invalid_tier = None
    stored_tier = (stored.get("tier") or "").strip() or None
    if stored_tier and stored_tier not in LOCAL_PRESETS:
        logger.warning(
            "stored managed-AI tier %r is not a known preset; falling back",
            stored_tier)
        invalid_tier = stored_tier
        stored_tier = None

    if stored_tier:
        tier, tier_source = stored_tier, "stored"
    else:
        env_tier = (os.getenv("MANAGED_OLLAMA_PRESET") or "").strip() or None
        if env_tier and env_tier not in LOCAL_PRESETS:
            logger.warning(
                "MANAGED_OLLAMA_PRESET=%r is not a known preset; falling back",
                env_tier)
            env_tier = None
        if env_tier:
            tier, tier_source = env_tier, "env"
        else:
            tier, tier_source = "balanced", "default"

    invalid_kind = None
    stored_kind = (stored.get("accelerator_kind") or "").strip().lower() or None
    if stored_kind:
        try:
            from services.managed_ai_stacks import normalize_kind
            stored_kind = normalize_kind(stored_kind)
        except Exception:  # noqa: BLE001
            pass

    if stored_kind:
        kind, kind_source = stored_kind, "stored"
    else:
        env_kind = (os.getenv("MANAGED_AI_ACCELERATOR_KIND") or "").strip().lower() or None
        if env_kind:
            kind, kind_source = env_kind, "env"
        else:
            kind, kind_source = _default_accelerator_kind_fallback(), "default"

    return ManagedAISettingResolved(
        tier=tier, tier_source=tier_source,
        accelerator_kind=kind, accelerator_kind_source=kind_source,
        invalid_stored_tier=invalid_tier,
        invalid_stored_accelerator_kind=invalid_kind,
    )


def resolve_managed_ai_setting(force: bool = False) -> "ManagedAISettingResolved":
    """§1.2 — the effective tier + accelerator kind, and which source won.

    Cached for `_SETTING_CACHE_TTL_S` seconds so one request's internal fan-out
    does not become a Vault round trip per call; `force=True` (used right after
    a write) bypasses that.
    """
    import time as _time
    now = _time.monotonic()
    cached = _managed_ai_setting_cache["value"]
    if (not force and cached is not None
            and (now - _managed_ai_setting_cache["ts"]) < _SETTING_CACHE_TTL_S):
        return cached
    resolved = _resolve_managed_ai_setting_uncached()
    _managed_ai_setting_cache["value"] = resolved
    _managed_ai_setting_cache["ts"] = now
    return resolved


def _invalidate_managed_ai_setting_cache() -> None:
    _managed_ai_setting_cache["value"] = None
    _managed_ai_setting_cache["ts"] = 0.0


def _default_preset() -> str:
    """§1.3 — replaces the old import-time `DEFAULT_PRESET` constant.

    Every internal caller in this module used to read the module constant
    directly; they now call this, which re-resolves per request rather than
    freezing the answer at process start.
    """
    return resolve_managed_ai_setting().tier


def _default_accelerator_kind() -> str:
    return resolve_managed_ai_setting().accelerator_kind

# ---------------------------------------------------------------------------
# Tier capabilities — which tools a preset can actually serve, and how well
# ---------------------------------------------------------------------------
#
# Gating is driven by CAPABILITY, not preference. A tool whose prompt cannot
# fit in the tier's context is unavailable no matter what an operator wants:
# the wizard at 8K context has zero input budget, so "enabled" would mean
# "silently produces nothing". That is the only hard gate.
#
# Everything else is a WARNING, not a block. On CPU all five tools function;
# they are simply slow, and an operator who has chosen a CPU box knows that.
# Hiding them would be pretending the tier is broken when it is merely modest.

# Tools whose prompts carry the wizard-class fixed overhead. `adapter` and
# `repair` build container/code context the same way the wizard does; the
# conversational tools do not.
_LARGE_CONTEXT_TOOLS = {"wizard", "adapter", "repair"}


def tier_capabilities(preset: str, kind: Optional[str] = None) -> Dict[str, Any]:
    """What `preset` can serve, per tool, and what to warn about.

    Returns a verdict per tool rather than a bare allow-list, because "this is
    off" and "this is on but will take minutes per call" are different facts
    and an operator needs both.
    """
    cfg = LOCAL_PRESETS.get(preset) or LOCAL_PRESETS[_default_preset()]
    ctx = int(cfg.get("max_model_len", 0))
    on_cpu = (cfg.get("compute_target") or _LOCAL_BASE["compute_target"]) == "cpu"

    # The wizard's own arithmetic, not a duplicate of it.
    from node_builder.llm_budget import compute_budgets
    budget = compute_budgets(ctx)

    tools: Dict[str, Any] = {}
    for tool in TOOLS:
        warnings = []
        available = True

        if tool in _LARGE_CONTEXT_TOOLS and budget.input_budget_tokens <= 0:
            available = False
            warnings.append(
                f"{cfg['label']} serves {ctx} tokens of context; after the "
                f"{budget.output_reservation_tokens + budget.fixed_overhead_tokens}-token "
                f"prompt overhead there is no room left for input. This tool "
                f"needs at least {WIZARD_MIN_CONTEXT_TOKENS}.")
        elif tool in _LARGE_CONTEXT_TOOLS and budget.input_budget_tokens < 8000:
            warnings.append(
                f"Only {budget.input_budget_tokens} tokens "
                f"(~{budget.input_budget_chars} chars) of input budget — large "
                f"files will be truncated before the model sees them.")

        if on_cpu and available:
            # Measured, not estimated. granite3.3:2b on 4 CPU cores,
            # 2026-08-02: ~30s cold load, ~11 tok/s prefill, ~2.6-3.0 tok/s
            # generation. A short chat turn lands in 17-23s; a wizard-class
            # call with ~4.4k tokens of input did not finish inside 20 minutes.
            if tool in _LARGE_CONTEXT_TOOLS:
                warnings.append(
                    "On CPU this tool is impractical: a full-context call "
                    "measured over 20 minutes and did not complete. Usable for "
                    "trials, not for work.")
            else:
                warnings.append(
                    "Running on CPU: a short exchange measured 17-23s. "
                    "Functional, not fast.")

        if not cfg.get("vision", False):
            warnings.append(f"{cfg['served_name']} is text-only — image inputs are ignored.")

        tools[tool] = {"available": available, "warnings": warnings}

    return {
        "preset": preset,
        "label": cfg["label"],
        "context_tokens": ctx,
        "input_budget_tokens": budget.input_budget_tokens,
        "compute_target": "cpu" if on_cpu else "gpu",
        "serving_stack": cfg.get("serving_stack", "vllm"),
        "vision": bool(cfg.get("vision", False)),
        # THE MODEL'S FOOTPRINT, which is not the card you need. A 4 GB
        # estimate on a 4 GB card does not fit: the driver, the KV cache at
        # full context and allocator fragmentation all come out of the same
        # budget, and cards sold as "8 GB" rarely expose 8 GB to a process.
        # Reporting the estimate as the requirement is how someone buys
        # hardware that OOMs on first load.
        "vram_est_gb": cfg.get("vram_est_gb", 0),
        # The card floor. Until now this lived only inside the human label
        # ("Minimal (5 GB GPU)"), so nothing could read it without parsing
        # prose.
        "vram_floor_gb": cfg.get("vram_floor_gb", cfg.get("vram_est_gb", 0)),
        "min_ram_gb": cfg.get("min_ram_gb"),
        "concurrent_users": cfg.get("concurrent_users", 1),
        "tools": tools,
        "unavailable_tools": sorted(t for t, v in tools.items() if not v["available"]),
        # per-rung-reasoner §2.3 — what this rung ACTUALLY requires.
        #
        # `vram_floor_gb` alone answers an operator's question only if they also
        # know how many such cards to buy. This rung may provision one stack or
        # two, and two may be sized to share a card or to take one each — a
        # difference that previously existed only as a `device_ids` pin inside a
        # compose file, readable by nobody.
        #
        # Also reports whether this rung reasons at all. The ladder used to
        # imply one reasoner everywhere because there was one; now a rung may
        # legitimately have none, and silence would read as an oversight rather
        # than as the answer.
        **_rung_requirement(preset, kind),
    }


def _rung_requirement(preset: str, kind: Optional[str] = None) -> Dict[str, Any]:
    """Hardware requirement and reasoning availability for one rung.

    Best-effort: this enriches a report, and a failure to compute it must never
    take out the capabilities call that the settings UI depends on.
    """
    try:
        from services.managed_ai_stacks import check_ladder_integrity
        row = next((r for r in check_ladder_integrity([preset], kind)), None)
    except Exception:  # noqa: BLE001
        logger.debug("rung requirement unavailable for %s", preset, exc_info=True)
        return {}
    if row is None:
        return {}

    reasoner = (LOCAL_PRESETS.get(preset) or {}).get("reasoner")
    return {
        "cards_required": row["cards_required"],
        "shares_one_card": row["shares_one_card"],
        "hardware_requirement": row["requirement"],
        "total_demand_gb": row["total_demand_gb"],
        "stacks": row["stacks"],
        "reasoning": {
            "available": reasoner is not None,
            "model": (reasoner or {}).get("served_name"),
            "note": (None if reasoner is not None else
                     "This tier does not provide a separate reasoning model. "
                     "Reasoning-heavy work is served by the generator, or by a "
                     "higher tier."),
        },
    }


# Shared constants — every preset inherits these. The provider is now
# openai_compatible (vLLM serves pure OpenAI-compat at /v1/*), not
# ollama. Base URL uses the "ollama" hostname alias during the cutover
# so old code paths don't break.
_LOCAL_BASE = {
    "provider": "openai_compatible",
    # Base URL is the vLLM root — do NOT include `/v1`. `llm_client.py`
    # appends `/v1/chat/completions` itself. Including `/v1` here causes
    # a double-prefix (`/v1/v1/chat/completions`) and a hard 404 on
    # every wizard call.
    "base_url": "http://ollama:11434",
    "keep_alive": "forever",   # no-op for vLLM but surfaces in UI for compat
    "compute_target": "gpu",
}


# ---------------------------------------------------------------------------
# Model catalog — the "WHICH local model" axis (orthogonal to LOCAL_PRESETS,
# which are devstral VRAM/context tiers). Lets an admin route different
# tools at different local models (e.g. fast generator vs strong reasoner).
# Each entry is a servable OpenAI-compatible endpoint. `available=False`
# entries still appear in the picker but are flagged "not served yet" —
# the model's vLLM instance hasn't been stood up. Provenance: aligned
# vendors only (no China/Russia origin) — surfaced for audit.
# ---------------------------------------------------------------------------
def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


MODEL_CATALOG: Dict[str, Dict[str, Any]] = {
    "devstral-small-2": {
        "label": "Devstral Small 2 (24B) — fast generalist coder",
        "served_name": "devstral-small-2",
        "base_url": _LOCAL_BASE["base_url"],          # the existing managed vLLM
        "vendor": "Mistral AI (FR)",
        "role_hint": "generation (every build — wants low latency)",
        "vram_est_gb": 22,
        # The CARD needed, not the weights: `compact` is the smallest rung that
        # serves this model, and its floor is 15.
        "vram_floor_gb": 15.0,
    },
    # Long-context reasoner on the 2nd vLLM (GPU 1). gpt-oss-20b chosen
    # over Llama-3.3-70B for a SINGLE card: ~16 GB MXFP4 weights leave
    # room for full 128K context, whereas the 70B capped at ~16K without
    # 2-GPU tensor-parallel. Flip available via LLM_BIG_AVAILABLE once
    # my_vllm_big is healthy.
    "gpt-oss-20b": {
        "label": "Long-context reasoner (128K)",
        "served_name": os.getenv("LLM_BIG_SERVED_NAME", "gpt-oss-20b"),
        "base_url": os.getenv("LLM_BIG_BASE_URL", "http://vllm-big:8000"),
        "vendor": "OpenAI (US)",
        "role_hint": "assistant / log reading / workflow planning / repair",
        "vram_est_gb": 16,
        "max_model_len": 131072,
        # Reasoning model: emits a hidden chain-of-thought into a separate
        # channel that counts against max_tokens BEFORE any answer tokens.
        # Callers must budget extra completion headroom (see support_chat
        # _REASONING_HEADROOM_TOKENS) or the answer comes back empty.
        "reasoning": True,
        # §1.4 — `available` USED to be
        #     os.getenv("LLM_BIG_AVAILABLE", "true") not in ("0","false","no")
        # which defaults to TRUE, so every deployment that never set the flag
        # advertised this model as servable and DEFAULT_MODEL then resolved
        # every reasoning tool to it. On a 16 GB box with no second vLLM that is
        # a model nothing can run, and the failure only appears at call time.
        # Availability is now COMPUTED from fleet capacity — see
        # `model_availability()`.
        #
        # The CARD this needs, which is not `vram_est_gb`: 16 GB of MXFP4
        # weights plus KV at 128K does not fit a 16 GB card. Same 22.5 figure
        # the reasoner floor has always used.
        "vram_floor_gb": 22.5,
    },
}
# Default model for any tool that hasn't explicitly picked one. Prefer
# the long-context reasoner (gpt-oss-20b) whenever it's actually served
# — it's MoE (~3.6B active), so it's not slower than devstral-24B-dense
# despite the longer context + reasoning, and it's aligned (OpenAI, US).
# Fall back to devstral on deployments without the 2nd vLLM so tools
# don't resolve to a dead endpoint.
# The safe fallback when fleet capacity cannot be consulted at all.
#
# NOT the largest or the newest — the one with the SMALLEST card requirement, so
# a deployment that cannot answer "what can we host" still resolves to the model
# most likely to run. The previous constant preferred gpt-oss-20b on the
# strength of an env var defaulting to true, which is how tools came to point at
# a model this hardware cannot serve.
FALLBACK_MODEL = min(
    MODEL_CATALOG, key=lambda k: MODEL_CATALOG[k].get("vram_floor_gb", 1e9))


def model_availability(key: str, db=None) -> Dict[str, Any]:
    """Can anything in the FLEET serve this model, and if not, why not.

    §1.1/§1.2/§1.3. Availability was previously an environment variable, which
    describes neither the fleet nor reality. It is now derived from the same
    accelerator-fit path placement uses, across every licensed worker — because
    work runs on workers, not on the manager. A model no single worker can host
    is unservable however much memory this box has; one some worker can host is
    servable however little it has.

    Capacity already reserved by running stacks is excluded, so a card spoken
    for is not offered twice — that is `_managed_ai_placement`'s job and the
    reason this reuses it rather than reading device totals directly.

    The reason distinguishes causes that need different responses: no worker has
    capacity (buy or free hardware) versus the model not being catalogued at all
    (a configuration error). "Unavailable" alone sends someone to the wrong fix.
    """
    # mesh-native-llm-routing §4.2/§4.3 — the catalogue is descriptive only.
    # When a running stack serves the model, availability is the reachability
    # of THAT stack's endpoint, whether or not the catalogue knows the name —
    # and for a catalogued model, a stack actually serving it beats the
    # fleet-exercise estimate.
    view = _stack_model_view(key, db=db)
    if view is not None:
        return _stack_availability_verdict(key, view)

    entry = MODEL_CATALOG.get(key)
    if entry is None:
        return {"available": False, "cause": "not_catalogued",
                "reason": (f"{key!r} is not a known model and no running "
                           f"stack serves it.")}

    floor = float(entry.get("vram_floor_gb") or 0.0)
    if floor <= 0:
        return {"available": True, "cause": "no_accelerator_needed", "reason": None}

    own_session = False
    try:
        if db is None:
            from database import SessionLocal
            db = SessionLocal()
            own_session = True
        from routers.persistent_stacks import _managed_ai_placement
        from services.managed_ai_stacks import normalize_kind
        kind = normalize_kind(_default_accelerator_kind())
        verdict = _managed_ai_placement(db, {kind: [floor]}, None)
        return {
            "available": bool(verdict.get("placeable")),
            "cause": "ok" if verdict.get("placeable") else "no_capacity",
            "reason": None if verdict.get("placeable") else verdict.get("reason"),
            "required_gb": floor,
            "largest_available_gb": verdict.get("largest_available_gb"),
            "shortfall_gb": verdict.get("shortfall_gb"),
        }
    except Exception:  # noqa: BLE001
        # Never let a capacity lookup break model resolution — an LLM call must
        # not fail because the fleet could not be counted. Reported as unknown
        # rather than as available, so nothing downstream treats a lookup
        # failure as proof the model runs.
        logger.debug("fleet availability unavailable for %s", key, exc_info=True)
        return {"available": None, "cause": "unknown",
                "reason": "Fleet capacity could not be determined."}
    finally:
        if own_session and db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


def _stack_model_view(key: str, db=None) -> Optional[Dict[str, Any]]:
    """The running stack that serves `key`, or None.

    mesh-native-llm-routing §4.2 — the served identity comes from the STACK
    record, not the catalogue: a model may be in neither and still be served.
    Matches the declared reference (`x-mod.served.model`, first service) or any
    name the engine serves under (`--served-model-name` / `--model`). The
    catalogue is never consulted here — it enriches, it does not gate.
    """
    def _name_matches(ref: str) -> bool:
        if not ref:
            return False
        return key in (ref, ref.split(":", 1)[0])

    own_session = False
    try:
        if db is None:
            from database import SessionLocal
            db = SessionLocal()
            own_session = True
        from utils.paid_import import optional_paid
        from crud.ai_config import list_running_stacks, list_stacks_by_names
        _li = optional_paid("services.llm_idle")
        if _li is None:
            return None  # CE: llm_idle absent → no stack view
        _mai = optional_paid("services.managed_ai_stacks")
        if _mai is None:
            return None  # CE: managed_ai_stacks absent → no stack view
        llm_idle = _li
        ALL_STACK_NAMES = _mai.ALL_STACK_NAMES

        # A catalogued model may ALSO be served by a stack whose name is not
        # the managed pair (e.g. a tenant-deployed vLLM serving devstral), and
        # an uncatalogued one by any stack — so the candidate set is every
        # running stack, plus the managed pair (which may read `deploying` on
        # the row while its endpoint already answers).
        candidates = list_stacks_by_names(db, ALL_STACK_NAMES) + list_running_stacks(db)
        seen = set()
        for st in candidates:
            if st.status != "running" or st.id in seen:
                continue
            seen.add(st.id)
            declared = _served_model_ref(st.compose_spec)
            if (declared and _name_matches(declared)) or \
                    any(_name_matches(n) for n in llm_idle.served_models(st)):
                return _stack_model_view_entry(st, db)
        return None
    except Exception:  # noqa: BLE001
        # No stack view must not masquerade as "nothing serves this".
        logger.debug("stack model view unavailable for %s", key, exc_info=True)
        return None
    finally:
        if own_session and db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


def _stack_model_view_entry(st, db) -> Optional[Dict[str, Any]]:
    """{served_name, base_url, role} for one running stack that serves a model.

    CE guard (GT.D4b pattern, precedent routers/embedding_models.py:118
    `optional_paid("services.managed_ai_stacks")` + None-check): both modules
    below are paid and absent from the CE tree, so the import must not fire
    unguarded at this helper's CALL time - the caller's try/except would only
    catch it by luck. On CE the entry is None (fail soft: no stack view).
    """
    from utils.paid_import import optional_paid
    _li = optional_paid("services.llm_idle")
    _mai = optional_paid("services.managed_ai_stacks")
    if _li is None or _mai is None:
        return None

    served = _li.served_models(st)
    served_name = served[0] if served else None
    if st.name == _mai.GENERATOR_STACK_NAME:
        role = "generator"
    elif st.name == _mai.REASONER_STACK_NAME:
        role = "reasoner"
    else:
        role = "custom"

    # Where this stack answers from here — the same reachability the managed
    # role path uses, and nothing new.
    base_url = None
    if role in ("generator", "reasoner"):
        try:
            base_url = _role_base_url(role)
        except Exception:  # noqa: BLE001
            base_url = None
    if not base_url:
        base_url = _stack_custom_base_url(st, db)
    return {"served_name": served_name, "base_url": base_url,
            "role": role, "stack_name": st.name, "stack_id": st.id}


def _stack_custom_base_url(st, db) -> Optional[str]:
    """A reachable base URL for a NON-managed stack. Single copy lives in
    services.stack_model_endpoint (2400GX-ceLLM) so CE can resolve tenant
    models; this name stays for existing callers."""
    from services.stack_model_endpoint import custom_base_url
    return custom_base_url(st, db)



def _stack_availability_verdict(key: str,
                                 view: Dict[str, Any]) -> Dict[str, Any]:
    """Reachability of the serving stack decides, for a model a stack serves."""
    base_url = view.get("base_url")
    reachable = False
    if base_url:
        try:
            host = urllib.parse.urlparse(base_url).hostname
        except Exception:  # noqa: BLE001
            host = None
        if host:
            reachable = _host_resolves(host)
    return {
        "available": bool(reachable),
        "cause": "ok" if reachable else "endpoint_unreachable",
        "reason": (None if reachable else
                   f"No running stack for {key!r} answers at "
                   f"{base_url or 'an endpoint'} — the stack is down or its "
                   f"endpoint does not reach the manager."),
        "served_by": view.get("stack_name"),
        "base_url": base_url,
        "served_name": view.get("served_name"),
    }


# Per-tool preference when a tool has no explicit `local_model` pinned. The
# GENERATION tools want the fast generator; reasoning tools want the reasoner.
# This is a PREFERENCE, not a resolution — what actually gets used is whatever
# of these the fleet can serve (see `resolve_default_model`).
#
# The node-build generate stage runs in the worker's egress sandbox and reaches
# the LLM via the EXTERNAL endpoint, which serves ONLY devstral; gpt-oss has no
# external route. Encoding it here rather than as a Vault pin keeps builds
# working after a `down -v` redeploy that wipes Vault.
_GENERATOR_TOOLS = {"wizard"}
_TOOL_MODEL_PREFERENCE = {
    True: ["devstral-small-2", "gpt-oss-20b"],    # generator tools
    False: ["gpt-oss-20b", "devstral-small-2"],   # reasoning tools
}


def resolve_default_model(tool: Optional[str] = None, db=None) -> str:
    """The model a tool gets when it has not pinned one.

    §1.5 — walks the tool's preference order and returns the first the fleet can
    actually serve. A default must never land on a model nothing can run: that
    failure is invisible until a call is made, and then presents as the feature
    being broken rather than as a configuration nobody could have known was
    wrong.

    When capacity cannot be determined at all, falls back to the model with the
    smallest card requirement rather than to the most capable one.
    """
    prefs = _TOOL_MODEL_PREFERENCE[tool in _GENERATOR_TOOLS]
    unknown = False
    for key in prefs:
        verdict = model_availability(key, db=db)
        if verdict["available"] is True:
            return key
        if verdict["available"] is None:
            unknown = True
    if unknown:
        return FALLBACK_MODEL
    # Nothing in the catalogue can be served. Say so rather than silently
    # returning something that cannot run — the caller surfaces it.
    logger.warning(
        "No catalogued model can be served by this fleet; falling back to %s. "
        "Managed local mode will not work until the fleet can host a model.",
        FALLBACK_MODEL)
    return FALLBACK_MODEL


# Back-compat: `DEFAULT_MODEL` was a module constant resolved at import from an
# env var. Anything still reading it gets the safe fallback rather than a guess
# about hardware, and callers that can consult the fleet should use
# `resolve_default_model()` instead.
DEFAULT_MODEL = FALLBACK_MODEL


def _model_is_reasoning(model_name: str) -> bool:
    """Heuristic for self-hosted / advanced modes where the model is a free
    string rather than a catalog key. Reasoning models (gpt-oss, OpenAI
    o-series, qwq, deepseek-r1, …) emit a hidden chain-of-thought that eats
    the completion budget before the answer — callers need extra headroom."""
    m = (model_name or "").lower()
    return any(t in m for t in ("gpt-oss", "o1", "o3", "o4", "qwq", "-r1", "reason", "thinking"))


# ── model-selection-by-role §2: a tool binds to a ROLE ────────────────────
#
# Which tool wants reasoning is a property of the tool; which model provides it
# is a property of the tier. Binding a tool to a model name conflates them, and
# the conflation shows up as five tools needing to be re-pointed by hand every
# time the hardware tier changes.

# Tools whose work is reasoning-shaped. Everything else generates.
# The wizard's WORKFLOW BUILDER is planning-shaped (map a request onto the real
# node catalog), so it defaults to the reasoner (mesh-native-llm-routing D3).
# This is only the DEFAULT — an operator can bind the wizard tool to any model
# in AI Config. The wizard's node-build generate stage reaches the external
# endpoint separately and is unaffected by this role default.
# The node-build GENERATE stage ("wizard") is a GENERATION tool, not a reasoning
# one — it is in `_GENERATOR_TOOLS`, and the generate stage reaches the LLM via
# the external endpoint that serves only the generator (see the comment there).
# Listing it here too made `default_role_for_tool("wizard")` return "reasoner",
# so on a generator-only tier (e.g. `minimal`, which serves granite-2b) the role
# failed to resolve and fell back to the catalogue's `devstral-small-2` — a model
# the fleet can't place. The call still landed on granite-2b (base_url points at
# the served stack; llama.cpp ignores the model name), but the REPORTED identity
# was wrong and it spammed "No catalogued model can be served … Managed local
# mode will not work". Code REPAIR stays a reasoner (smart fixes); GENERATE does
# not. Pinned by test_a_tool_binds_to_a_role_not_a_model_name.
# "critic" is the wizard's REVIEW side — critique_script / critique_executability
# / critique_directions and the coder-stalled escalation all resolve tool="critic"
# (see node_builder.critic_synth + describe_synthesize._reasoner_generation_llm).
# It is a reasoning job exactly like repair, so it binds to the reasoner role and
# lands on the 8B reasoner stack (vllm-big:8000) rather than the 2b generator.
# Without this the wizard's coder AND its critic both resolved to the generator
# (ollama:11434, granite-2b) and the escalation was a silent no-op re-querying the
# 2b — the 8B reasoner sat idle. GENERATION (tool="wizard") stays a generator.
_REASONING_TOOLS = {"assistant", "support", "repair"}


def _wizard_critic_mode() -> str:
    """The operator's Node Wizard code-review setting — 'self' | 'reasoner' |
    'off' — from Dashboard AI settings (wizard tool's `critic_mode`). Default
    'self' (A/B testing 2026-09-04: a capable 8B coder self-reviews as well as a
    separate reasoner, faster, and frees the reasoner's VRAM). Never raises."""
    try:
        mode = (_read_tool_config("wizard") or {}).get("critic_mode")
    except Exception:  # noqa: BLE001
        mode = None
    return mode if mode in ("self", "reasoner", "off") else "self"


def default_role_for_tool(tool: Optional[str]) -> str:
    # The Node Wizard's REVIEW pseudo-tool ("critic") follows the operator's
    # critic_mode instead of a hardcoded binding: self-review takes the coder
    # (generator) model, a separate review takes the reasoner. ("off" is handled
    # by the enable gate in critic_synth, not here.)
    if tool == "critic":
        return "reasoner" if _wizard_critic_mode() == "reasoner" else "generator"
    return "reasoner" if tool in _REASONING_TOOLS else "generator"


def critic_env() -> Dict[str, str]:
    """Effective LLM env for the Node Wizard's REVIEW pass, honouring the
    operator's ``critic_mode`` (Dashboard → AI Config → Node Wizard → Review
    reasoner):

      - "self"     → the coder reviews its own output, so the review runs on the
                     WIZARD (generator) model — never a stored separate-reviewer
                     config. This is the case a bare ``_get_tool_config("critic")``
                     got wrong once an independent critic model was persisted: it
                     would use that model even though the operator asked for
                     self-review.
      - "reasoner" → "Separate reasoner model". Resolves the independently
                     configured reviewer (the ``critic`` pseudo-tool's own Vault
                     config: Provider / Model / Base URL / API Key / Temperature /
                     Max Tokens). When that tool is left in managed/local mode it
                     resolves, via the reasoner ROLE, to the active tier's
                     reasoner — the honest managed default.
      - "off"      → the review is disabled upstream (critic_synth._enabled), so
                     this is never reached; it degrades to the reasoner branch.

    Never raises — falls back to the wizard env on any failure so a review is
    attempted rather than silently skipped.
    """
    try:
        mode = _wizard_critic_mode()
        if mode == "self":
            return _resolved_effective_env("wizard")
        return _resolved_effective_env("critic")
    except Exception:  # noqa: BLE001 — a review is better than none
        try:
            return _resolved_effective_env("wizard")
        except Exception:  # noqa: BLE001
            return {}


def tier_roles(preset: Optional[str] = None,
               kind: Optional[str] = None) -> Dict[str, Any]:
    """What the active tier serves, keyed by role.

    §2.3 — a rung may serve no reasoner at all (`minimal` declares none), and a
    role the tier does not serve must not be offered. An option that cannot
    resolve is worse than an absent one: it looks like a working choice and
    fails at call time.

    `kind` was previously read as a free variable that did not exist here, so
    every call raised NameError, was swallowed by the except below, and this
    returned {} — making EVERY tier report that it serves nothing. The visible
    symptom was a tier announcing "serves no generator" directly above the
    generator it provisions.
    """
    name = preset or _default_preset()
    try:
        from services.managed_ai_stacks import stack_roles
        return {r["role"]: r for r in stack_roles(name, kind)}
    except Exception:  # noqa: BLE001
        logger.debug("tier roles unavailable for %s", name, exc_info=True)
        return {}


def resolve_role(tool: Optional[str], raw: dict) -> Dict[str, Any]:
    """Which model this tool's ROLE resolves to on the active tier.

    Returns {role, model, served_name, resolved, reason}. `resolved` False means
    the tier serves nothing for this role — reported rather than quietly
    swapped for the other role's model, because answering a reasoning request
    from the generator without saying so is a silent downgrade nobody consented
    to.
    """
    preset = raw.get("local_preset") or _default_preset()
    role = raw.get("local_role") or default_role_for_tool(tool)
    roles = tier_roles(preset)
    entry = roles.get(role)
    if entry is None:
        return {
            "role": role, "model": None, "served_name": None,
            "max_model_len": None, "vram_floor_gb": None, "resolved": False,
            "reason": (f"The {preset} tier serves no {role}. "
                       f"Choose a tier that does, or bind this tool to "
                       f"{'generation' if role == 'reasoner' else 'reasoning'}."),
        }
    return {"role": role, "model": entry.get("served_name"),
            "served_name": entry.get("served_name"),
            # The served model's OWN context and card floor, carried through so
            # the config surface reports the model actually being served rather
            # than a catalogue default (see _resolve_config). max_model_len is
            # what the stack was deployed with (--max-model-len), so it is the
            # authoritative context for the served model.
            "max_model_len": entry.get("max_model_len"),
            "vram_floor_gb": entry.get("vram_floor_gb"),
            # Where that role answers. Engine-independent: the stacks join the
            # platform network under the alias each role has always used, so a
            # rung swapping llama.cpp for vLLM changes nothing here.
            "base_url": _role_base_url(role),
            "resolved": True, "reason": None}


def _managed_key(role: Optional[str] = None) -> str:
    try:
        from services.managed_ai_stacks import managed_llm_api_key
        return managed_llm_api_key(role=role)
    except Exception:  # noqa: BLE001
        return ""


def is_managed_local_llm(base_url: str) -> bool:
    """Return True if the base_url points to a managed local LLM (internal aliases).

    The predicate checks if the URL's host matches one of the managed AI stack
    aliases (ollama, vllm-big) case-insensitively, ignoring the port and path.
    This is used to determine when to repoint remote-worker builds to the
    externally accessible URL rather than the internal service hostname.
    """
    try:
        parsed = urllib.parse.urlparse(base_url)
        host = parsed.hostname
        if not host:
            return False
        host_lower = host.lower()
        from services.managed_ai_stacks import _GGUF_ROLE_ALIASES
        return host_lower in (a.lower() for a in _GGUF_ROLE_ALIASES.values())
    except Exception:  # noqa: BLE001
        return False


def _role_base_url(role: str) -> Optional[str]:
    """Where this role's model actually answers, from here.

    The alias when the stack shares our network, and the hosting worker's
    advertised address when it does not. That second case is not optional:
    the assistant STREAMS, and a streaming reply cannot travel as a task
    result, so a model on another host needs a route or the assistant is
    simply down — which is what happened on 2026-08-13 when the pair moved to
    a remote worker and "Create a new node" started returning a network error.

    Reachability is VERIFIED, never assumed. A worker derives its advertised
    address from the route to the manager, which is the best guess the only
    informed party can make, and asymmetric routing or NAT can still make it
    undialable from here. An unverified address would turn a working alias
    into a black hole.
    """
    try:
        from services.managed_ai_stacks import (_GGUF_ROLE_ALIASES,
                                                _GGUF_ROLE_PORTS)
        alias = _GGUF_ROLE_ALIASES.get(role, "ollama")
        port = _GGUF_ROLE_PORTS.get(role, 11434)
        local = f"http://{alias}:{port}"
        if _host_resolves(alias):
            return local
        remote = _remote_role_base_url(role, port)
        return remote or local
    except Exception:  # noqa: BLE001 — no derivable endpoint: unknown, never invented
        logger.warning("ai_config: no role base_url derivable for role=%s "
                       "(8.3: a slot must not resolve to a hard-coded alias)",
                       role)
        return None


_HOST_RESOLVES_TTL_S = 30.0
_host_resolves_cache: dict = {}


def _host_resolves(host: str) -> bool:
    """Cached for a short TTL. A name that does NOT resolve costs ~0.5s in
    Docker's resolver, and one /ai-config/all load asks about the same few
    aliases ~20 times — uncached that was ~11s per page load. A stale entry is
    served while a background lookup refreshes it.

    BUG.B45 — the TTL alone was not enough. The ~0.5s figure in the note above
    is what a miss costs when the resolver answers promptly; on this fleet a
    name nothing resolves (a torn-down stack's alias, e.g. `vllm-big`) costs
    ~3.6s, because glibc's `getaddrinfo` has NO timeout knob and walks its
    full retry ladder against a host that is in neither /etc/hosts nor the
    Docker namespace. Nothing about that call is bounded, so a cold miss sat
    on the request path: the first Settings -> AI Configuration draw after an
    API restart took 10.6s for `/ai-config/all` alone (6 tools x the same
    dead aliases), against 0.05s once warm. A cold miss therefore now pays at
    most `_HOST_RESOLVE_PROBE_TIMEOUT_S` and takes the optimistic answer
    (the same answer the resolver-fault path already gives), while the probe
    keeps running and caches the real verdict for the next request.
    """
    import threading
    import time as _time

    hit = _host_resolves_cache.get(host)
    if hit:
        if _time.monotonic() - hit[1] >= _HOST_RESOLVES_TTL_S and host not in _host_resolves_refreshing:
            # Stale: answer from the last lookup and refresh off the request path.
            _host_resolves_refreshing.add(host)
            threading.Thread(target=_host_resolves_lookup, args=(host,), daemon=True).start()
        return hit[0]
    return _host_resolves_bounded(host, _HOST_RESOLVE_PROBE_TIMEOUT_S)


# The budget a COLD probe may spend before the request gives up waiting for it.
# Not an env var and not a new setting: the same module-level constant shape
# `_HOST_RESOLVES_TTL_S` and `_REMOTE_BASE_TTL_S` already use. It only caps the
# wait — a resolver that answers inside it still returns its real verdict.
_HOST_RESOLVE_PROBE_TIMEOUT_S = 1.0


def _host_resolves_bounded(host: str,
                           timeout: float = _HOST_RESOLVE_PROBE_TIMEOUT_S) -> bool:
    """BUG.B45 — the cold-path wrapper: pay at most `timeout` for a first answer.

    `socket.getaddrinfo` cannot be given a timeout, so the only way to bound it
    is to run it elsewhere and stop waiting for it. The probe keeps running
    after the timeout and caches the real verdict, so exactly ONE request pays
    the budget per host per TTL instead of every tool row in the page.
    """
    import threading
    import time as _time

    verdict: Dict[str, bool] = {}
    _host_resolves_refreshing.add(host)
    worker = threading.Thread(target=_host_resolves_probe, args=(host, verdict),
                              daemon=True)
    worker.start()
    try:
        worker.join(timeout)
    except Exception:  # noqa: BLE001 — a stubbed Thread has no join
        pass
    if verdict:
        return verdict["ok"]

    # The probe is still walking the resolver. Answer optimistically — the
    # resolver-fault path already treats an unanswerable lookup as "resolves",
    # and a wrong-optimistic answer only means one request shows an endpoint as
    # reachable that the next request (TTL 30s, probe ~3.6s) reports as not.
    # Stamped into the cache so the rest of the page load does not each pay the
    # budget again; the running probe overwrites it with the real verdict.
    _host_resolves_cache[host] = (True, _time.monotonic())
    return True


def _host_resolves_probe(host: str, verdict: Dict[str, bool]) -> None:
    verdict["ok"] = _host_resolves_lookup(host)


_host_resolves_refreshing: set = set()


def _host_resolves_lookup(host: str) -> bool:
    import socket
    import time as _time

    try:
        socket.getaddrinfo(host, None)
        ok = True
    except socket.gaierror:
        ok = False
        logger.debug("resolver: %s does not resolve", host)
    except Exception:  # noqa: BLE001
        _host_resolves_refreshing.discard(host)
        return True          # a resolver fault is not an answer (and not cached)
    _host_resolves_cache[host] = (ok, _time.monotonic())
    _host_resolves_refreshing.discard(host)
    return ok


_REMOTE_BASE_TTL_S = 60.0
_remote_base_cache: dict = {}


def _remote_role_base_url(role: str, port: int) -> Optional[str]:
    """`http://<host>:<port>` for the worker hosting this role, if we can
    actually open a socket to it.

    Tries the stack's mesh MagicDNS name first (`_stack_mesh_endpoints`),
    then falls back to the hosting worker's advertised address — both
    verified with the same 2-second socket-connect probe. Cached together
    under one `_remote_base_cache[role]` slot."""
    import socket
    import time as _time

    hit = _remote_base_cache.get(role)
    if hit is not None and (_time.monotonic() - hit[0]) < _REMOTE_BASE_TTL_S:
        return hit[1]

    answer = None
    try:
        import models
        from database import SessionLocal
        from utils.paid_import import optional_paid

        # managed-ai-self-deploy is a Paid feature: `services/managed_ai_stacks.py`
        # is denied by open-core/manifest.yaml, so the CE carve strips it and this
        # module is absent at call time. Guarded (optional_paid) rather than raw:
        # with no managed stacks there is no stack to dial, so the base url DEGRADES
        # to None — the same "no answer" posture the existing handler already
        # caches — rather than ImportError-500. On the paid tree `mas` is the
        # module itself and every path below is byte-for-byte the existing one.
        mas = optional_paid("services.managed_ai_stacks")
        if mas is None:
            return None  # CE: managed_ai_stacks absent → no managed stack to dial
        GENERATOR_STACK_NAME = mas.GENERATOR_STACK_NAME
        REASONER_STACK_NAME = mas.REASONER_STACK_NAME
        name = GENERATOR_STACK_NAME if role == "generator" else REASONER_STACK_NAME
        db = SessionLocal()
        try:
            from crud.ai_config import get_running_stack_by_name, get_worker_license_by_worker_name
            stack = get_running_stack_by_name(db, name)
            worker = (get_worker_license_by_worker_name(db, stack.target_worker)
                      if stack and stack.target_worker else None)
            addr = ((worker.hardware_specs or {}).get("advertise_address")
                    if worker is not None else None)
            # The stack's own MagicDNS name(s), keyed by compose service —
            # "reasoner"/"generator" for the vLLM composers, "llm" for the
            # llama.cpp/GGUF one (services/managed_ai_stacks.py). Import
            # lazily to avoid a module-load cycle with persistent_stacks.
            magic_host = None
            if stack is not None:
                try:
                    from routers.persistent_stacks._shared import _stack_mesh_endpoints
                    endpoints = _stack_mesh_endpoints(stack)
                    for key in (role, "llm"):
                        entry = endpoints.get(key)
                        if entry and entry.get("magicdns_name"):
                            magic_host = entry["magicdns_name"]
                            break
                except Exception:  # noqa: BLE001
                    logger.debug("mesh endpoint lookup failed for %s", role, exc_info=True)
        finally:
            db.close()

        # MagicDNS FIRST: the tailnet overlay name for this stack's service.
        # Tried before the worker's advertise_address because the mesh
        # overlay is the route meant to survive host moves; the advertise
        # address is a same-LAN best guess. The API container commonly runs
        # on Docker Desktop and cannot dial 100.64/10 tailnet addresses at
        # all, so this connect attempt failing is the common case — it must
        # just fall through quickly (same 2s timeout, no retries), not block
        # on the eventual advertise-address fallback below.
        if magic_host:
            s = socket.socket()
            s.settimeout(2.0)
            try:
                s.connect((magic_host, int(port)))
                answer = f"http://{magic_host}:{port}"
            except Exception:  # noqa: BLE001 — MagicDNS may not resolve/route from here
                logger.debug("managed-AI %s: magicdns %s:%s not reachable",
                             role, magic_host, port)
            finally:
                s.close()

        if answer is None and addr:
            s = socket.socket()
            s.settimeout(2.0)
            try:
                s.connect((addr, int(port)))
                answer = f"http://{addr}:{port}"
            except Exception:  # noqa: BLE001 — advertised is not the same as reachable
                logger.debug("managed-AI %s: %s:%s advertised but not reachable",
                             role, addr, port)
            finally:
                s.close()
    except Exception:  # noqa: BLE001
        logger.debug("remote base-url resolution failed for %s", role, exc_info=True)

    _remote_base_cache[role] = (_time.monotonic(), answer)
    return answer


def migrate_pin_to_role(raw: dict, tool: Optional[str] = None) -> Dict[str, Any]:
    """§2.4/§2.5 — map an existing `local_model` pin onto the role it plays.

    A pin naming the tier's generator becomes a generator binding; one naming
    its reasoner becomes a reasoner binding. A pin matching NEITHER is reported
    unresolved rather than coerced onto the nearest role — the operator chose
    that model deliberately, and silently substituting hides that their choice
    no longer applies.
    """
    pin = raw.get("local_model")
    if not pin:
        return {"role": raw.get("local_role") or default_role_for_tool(tool),
                "migrated": False, "reason": None}

    preset = raw.get("local_preset") or _default_preset()
    served = MODEL_CATALOG.get(pin, {}).get("served_name") or pin
    for role, entry in tier_roles(preset).items():
        if entry.get("served_name") == served or entry.get("served_name") == pin:
            return {"role": role, "migrated": True, "reason": None}
    return {
        "role": None, "migrated": False,
        "reason": (f"This tool is pinned to {pin!r}, which the {preset} tier "
                   f"does not serve in any role. It has not been changed for "
                   f"you — pick a role, or a tier that serves that model."),
    }


def _local_model_entry(raw: dict, tool: Optional[str] = None,
                       db=None) -> Dict[str, Any]:
    """The catalog entry a tool's config points at in Local mode. Falls
    back to the tool's default model when unset or unknown (back-compat: an
    existing config with no `local_model` resolves exactly as before, except
    generator tools now default to devstral — see resolve_default_model).

    mesh-native-llm-routing §4.3 — a model no catalogue entry describes but a
    running stack serves still gets an entry, so it can be bound and resolved:
    the served name IS the label, and the catalogue only enriches when it
    happens to know the model."""
    key = raw.get("local_model") or resolve_default_model(tool)
    entry = MODEL_CATALOG.get(key)
    if entry is not None:
        return entry
    view = _stack_model_view(key, db=db)
    if view is not None:
        return {
            "label": key,
            "served_name": view.get("served_name") or key,
            # 8.3b — no invented endpoint: a stack view that carries no base_url
            # has no known answer point, so the key stays unset (None) and the
            # config surface says "no endpoint" instead of pointing at ollama.
            "base_url": view.get("base_url"),
            "vendor": "served by stack %s" % view.get("stack_name"),
            "role_hint": "served by a running stack",
        }
    return MODEL_CATALOG[DEFAULT_MODEL]


def _preset_for(raw: dict) -> Dict[str, Any]:
    """Return the preset dict a tool's config points at.

    Any tool running in Local mode uses the SAME preset — vLLM is a
    single-model server, we can't give different tools different local
    models. The preset stored on any one tool's config wins; typically
    admins set them identically via the Settings UI.
    """
    name = raw.get("local_preset") or _default_preset()
    return LOCAL_PRESETS.get(name) or LOCAL_PRESETS["balanced"]


def _preset_name_for(raw: dict) -> str:
    name = raw.get("local_preset") or _default_preset()
    return name if name in LOCAL_PRESETS else "balanced"


class ToolAIConfig(BaseModel):
    """Per-tool AI config. api_key is masked as a bool on GET (never returned raw)."""
    execution_mode: ExecutionMode = "local"
    provider: Optional[str] = None
    model: Optional[str] = None
    api_key: bool = False
    base_url: Optional[str] = None
    # compute_target is preserved for Advanced mode and forward-compat with
    # Service Nodes when Local mode returns.
    compute_target: Literal["cpu", "gpu", "auto"] = "cpu"
    # keep_alive is still meaningful for Self-hosted Ollama (passed through
    # to the user's Ollama instance).
    keep_alive: Literal["off", "5m", "30m", "1h", "forever"] = "forever"
    temperature: str = "0.3"
    max_tokens: str = "4096"
    # Local-mode preset (Compact/Balanced/Capable). Only meaningful when
    # execution_mode == "local"; otherwise echoed back for UI stability.
    local_preset: Optional[PresetName] = None
    # Local-mode model catalog key (which model this tool routes to).
    # None resolves to the tool's role — see `local_role`.
    local_model: Optional[str] = None
    # model-selection-by-role §2.1 — WHICH ROLE this tool wants from the active
    # tier, rather than which model it wants.
    #
    # A model NAME goes stale the moment the tier changes, and re-pointing every
    # tool by hand after a tier change is precisely the manual step this
    # removes. A role stays correct across tier changes by construction: the
    # tier says what serves generation and what serves reasoning, and the tool
    # says which of those it needs.
    local_role: Optional[Literal["generator", "reasoner"]] = None
    # Read-only: what the role currently resolves to, and why it does not when
    # it cannot. Surfaced so a tool bound to a role the tier stopped serving
    # reads as unresolved rather than silently answering from something else.
    resolved_role_model: Optional[str] = None
    role_unresolved_reason: Optional[str] = None
    # Self-hosted embedding model (name of a model uploaded via
    # /admin/embedding-models → MinIO platform-models/embeddings).
    #  • Node Wizard uses `embedding_model` → the wizard pgvector store.
    #  • AI Assistant uses a store SUB-CHOICE: `embedding_store` picks which
    #    backend the pin applies to, and the per-store pins hold the model.
    # Empty/None => platform default for that store.
    embedding_model: Optional[str] = None
    embedding_store: Optional[Literal["user_rag", "docs"]] = None
    embedding_model_user_rag: Optional[str] = None
    embedding_model_docs: Optional[str] = None
    # Resolved num_ctx from the preset — read-only, surfaced so the UI
    # can tell the admin exactly how large a context the model will
    # allocate VRAM for.
    num_ctx: Optional[int] = None
    # WIZARD ONLY — how the Node Wizard's code REVIEW (Critic D + the coder-stall
    # escalation) is served. Operator-selectable in Dashboard AI settings; other
    # tools ignore it.
    #   "self"     — the coder reviews its own output (critic == generator model).
    #                Default: A/B testing (2026-09-04) showed a capable 8B coder
    #                self-reviews as well as a separate reasoner, faster, and frees
    #                the reasoner's VRAM.
    #   "reasoner" — a distinct reasoner model reviews (the tier's reasoner role).
    #   "off"      — no LLM review; deterministic guards only.
    critic_mode: Literal["self", "reasoner", "off"] = "self"
    # mesh-native-llm-routing §4.8 — opt-in per-tool LLM fallback: when the
    # tool's own binding is unreachable, dispatch through fallback_tool's
    # binding and announce the substitution.
    fallback_enabled: bool = False
    fallback_tool: Optional[str] = None
    # GE.AIC.1-3 — provider == "stack" points this slot at a running
    # PersistentStack instead of Base URL/API Key/Compute Target/Keep Alive.
    # See services/ai_stack_provider.py for the full contract.
    stack_id: Optional[int] = None
    served_model: Optional[str] = None
    # Embedding-slot provider switch — "platform" (self-hosted from the
    # Models tab, today's default) or "stack". One pair per store: the
    # wizard learning store, and the assistant's two independent stores.
    embedding_provider: Optional[Literal["platform", "stack"]] = None
    embedding_stack_id: Optional[int] = None
    embedding_provider_user_rag: Optional[Literal["platform", "stack"]] = None
    embedding_stack_id_user_rag: Optional[int] = None
    embedding_provider_docs: Optional[Literal["platform", "stack"]] = None
    embedding_stack_id_docs: Optional[int] = None


class ToolAIConfigUpdate(BaseModel):
    """All fields optional; only set values are applied. api_key here is the raw string."""
    execution_mode: Optional[ExecutionMode] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    compute_target: Optional[Literal["cpu", "gpu", "auto"]] = None
    keep_alive: Optional[Literal["off", "5m", "30m", "1h", "forever"]] = None
    temperature: Optional[str] = None
    max_tokens: Optional[str] = None
    local_preset: Optional[PresetName] = None
    local_model: Optional[str] = None
    local_role: Optional[Literal["generator", "reasoner"]] = None
    critic_mode: Optional[Literal["self", "reasoner", "off"]] = None
    embedding_model: Optional[str] = None
    embedding_store: Optional[Literal["user_rag", "docs"]] = None
    embedding_model_user_rag: Optional[str] = None
    embedding_model_docs: Optional[str] = None
    # mesh-native-llm-routing §4.8 — opt-in per-tool LLM fallback.
    # fallback_tool="" clears the target (→ fallback disabled).
    fallback_enabled: Optional[bool] = None
    fallback_tool: Optional[str] = None
    # GE.AIC.1-3 — see ToolAIConfig above.
    stack_id: Optional[int] = None
    served_model: Optional[str] = None
    embedding_provider: Optional[Literal["platform", "stack"]] = None
    embedding_stack_id: Optional[int] = None
    embedding_provider_user_rag: Optional[Literal["platform", "stack"]] = None
    embedding_stack_id_user_rag: Optional[int] = None
    embedding_provider_docs: Optional[Literal["platform", "stack"]] = None
    embedding_stack_id_docs: Optional[int] = None


def _get_vault_token() -> Optional[str]:
    """Vault token: agent file (paid) else the CE OpenBao AppRole login.

    CE ships no Vault Agent (open-core/overlay/docker-compose.yml:353;
    open-core/tests/test_ce_clean_checkout.py:264), so the hardcoded
    /run/vault/token could never exist there — every AI-config save raised
    RuntimeError -> HTTP 500. Resolution now lives in ONE place,
    utils.openbao_client.get_vault_token (agent token first, so paid is
    byte-identical and the AppRole branch never runs there).
    """
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
    except Exception as e:
        # FIX-AI-CONFIG-VAULT-404-QUIET: a plain HTTP 404 is the "missing path"
        # case this docstring always promised (a fresh CE has never written
        # platform/ai-config/*, so every poll read is a 404 — 700+ WARNING
        # lines/day/box on vm and xeon). Log it at DEBUG, PATH ONLY, never the
        # exception text (a VaultError body can carry response fragments).
        from utils.vault_client import _not_found
        if _not_found(e):
            logger.debug("ai_config Vault path missing for %s", path)
            return {}
        # FIX-AICONFIG-REVIEW-CHANGES item 4: a CE 403 (policy denial) — and
        # every other failure (5xx, unreachable, sealed) — must stay visible.
        # Operation + exception CLASS only; the message never carries values.
        logger.warning("ai_config Vault read failed for %s: %s", path, type(e).__name__)
        return {}


def _write_vault_path(path: str, config: dict):
    """Write a Vault KV v2 secret.

    FIX-AI-CONFIG-VAULT-UNAVAILABLE-TYPED-503 (LEDGER-PARTIAL-1293 B-Check 3):
    the no-token case used to raise a bare RuntimeError, which FastAPI turns
    into an opaque 500 "Internal server error" — the Settings UI could not tell
    the admin that the secrets service is simply not usable on this host. Raise
    a typed 503 instead, same detail shape as the repo's other typed errors
    (node_builder/build_dag/submit.py:116-118 build_farm_unavailable). No
    secret material in the message.
    """
    from utils.vault_client import write_kv_v2
    token = _get_vault_token()
    if not token:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "vault_unavailable",
                "message": "The secrets service has no agent token on this host; "
                           "check that OpenBao/Vault is running and unsealed, then try again",
            },
        )
    write_kv_v2(path, token, config)


def _vault_path_for(tool: str) -> str:
    return f"platform/ai-config/{tool}"


# ---------------------------------------------------------------------------
# Managed-Ollama residency reconciler
# ---------------------------------------------------------------------------
# When any of the three tools has execution_mode == "local", it points at
# MOD's compose-level Ollama (http://ollama:11434) with the shared
# devstral:latest model. We want that model to be HOT in VRAM the moment a
# tool switches to local — not lazy-loaded on first request — and we want
# it EVICTED when the last tool switches away. Ollama's preload is a
# single `POST /api/generate` with `keep_alive: -1` (pin forever) and
# eviction is the same call with `keep_alive: 0`.
#
# This only governs the managed Ollama. Self-hosted Ollama points at a
# user-operated URL and we don't touch its residency.

_MANAGED_OLLAMA_URL = os.getenv("MANAGED_OLLAMA_URL", "http://ollama:11434")


def _managed_model_served_name(preset_name: str) -> str:
    """The --served-model-name vLLM uses for a preset, which becomes the
    value of the `model` field in /v1/chat/completions requests."""
    preset = LOCAL_PRESETS.get(preset_name) or LOCAL_PRESETS["balanced"]
    return preset["served_name"]


# The `_managed` Vault doc tracks which preset the deployment is
# currently running. On vLLM this is informational only — the actual
# running model is whatever docker compose launched; the API writes
# here so the Settings UI can warn when a saved preset differs from
# the deployed one (indicating the admin needs to restart compose to
# apply their change).
_MANAGED_STATE_PATH = "platform/ai-config/_managed"


def _any_tool_in_local_mode(skip_tool: str = None, override: dict = None) -> bool:
    """True if any of the three tools is currently configured in local mode.

    `skip_tool` + `override` let the caller treat the *currently-being-saved*
    tool's in-memory config as authoritative instead of re-reading it from
    Vault (avoids a race where Vault replication hasn't caught up yet).
    """
    for t in TOOLS:
        cfg = override if (skip_tool and t == skip_tool) else _read_tool_config(t)
        if (cfg.get("execution_mode") or "local") == "local":
            return True
    return False


def is_system_managed_effective() -> bool:
    """§5.5 — whether System managed is in effect, read off the tools.

    System managed is a client-side umbrella with no server flag (the master
    toggle lives in the browser); what the server sees is that the mode sets
    EVERY tool uniformly to managed local. So the server's honest reading of
    "System managed is on" is: every tool is in local mode. Used to lock the
    settings the mode owns — the residency preference is the single control it
    presents, and a per-stack pin or idle override must not be allowed to
    contradict it while the mode is in effect.
    """
    return all((_read_tool_config(t).get("execution_mode") or "local") == "local"
               for t in TOOLS)


def _active_preset_for_managed_ollama(skip_tool: str = None, override: dict = None) -> Optional[str]:
    """Pick the preset to pin on the managed Ollama.

    Rule: the first tool that is in local mode decides. In practice most
    deployments keep all three tools on the same preset so this is
    unambiguous; mixed presets fall back to whichever tool is evaluated
    first (wizard → adapter → assistant) to avoid churn from swapping
    models back and forth on each save.
    """
    for t in TOOLS:
        cfg = override if (skip_tool and t == skip_tool) else _read_tool_config(t)
        if (cfg.get("execution_mode") or "local") == "local":
            return _preset_name_for(cfg)
    return None


def _reconcile_managed_ollama_residency(
    should_be_resident: bool, target_preset: Optional[str] = None,
) -> None:
    """Record the preset the admin wants; do NOT hot-swap the model.

    vLLM (unlike Ollama) serves one model per container, chosen at
    container start from MANAGED_OLLAMA_PRESET env. Changing preset is
    a deployment-time operation:

        # On the host:
        export MANAGED_OLLAMA_PRESET=production
        docker compose up -d --force-recreate vllm

    The API can't do that itself (no Docker socket, no privilege), so
    this function only persists the admin's intent to Vault. The
    Settings UI reads _managed.preset and compares against the running
    vLLM's served model list (via /v1/vllm-health) to surface a "pending
    restart" badge when they differ.
    """
    target_name = target_preset if should_be_resident else None
    try:
        _write_vault_path(_MANAGED_STATE_PATH, {"preset": target_name})
        logger.info(
            "Managed LLM preset intent saved: %s (resident=%s) — "
            "apply with: docker compose up -d --force-recreate vllm",
            target_name, should_be_resident,
        )
    except Exception as e:
        logger.warning("Failed to persist managed LLM preset intent: %s", e)


def _read_tool_config(tool: str) -> dict:
    """Read a tool's raw Vault config, falling back to the legacy shared entry
    only if the tool-specific entry is empty."""
    data = _read_vault_path(_vault_path_for(tool))
    if data:
        return data

    # separate-reasoner-model §1 — the "critic" pseudo-tool is NEW; it has no
    # legacy shared entry to inherit. Seeding it from the old single-entry config
    # would make an unconfigured separate reviewer masquerade as the legacy
    # wizard model instead of falling back to the tier's reasoner role. So an
    # empty critic config stays empty (→ managed/local → reasoner role).
    if tool in PSEUDO_TOOLS:
        return {}

    # Migration: first-ever read returns empty — seed from legacy single-entry
    # config if present. Does NOT persist; on first successful PUT the new
    # per-tool path is created.
    legacy = _read_vault_path("platform/ai-config")
    if legacy:
        seeded = dict(legacy)
        seeded.setdefault("execution_mode", "local")
        return seeded

    return {}


def _resolve_config(tool: str, raw: dict) -> ToolAIConfig:
    """Project a raw Vault dict into a response model, masking api_key.

    For Local mode the preset decides model/num_ctx and the UI shows
    those alongside the dropdown. For other modes the persisted values
    are echoed back as before.
    """
    mode = raw.get("execution_mode", "local")
    if mode not in ("local", "self_hosted", "cloud", "advanced"):
        mode = "local"

    resolved_local_model = None
    if mode == "local":
        resolved_provider = _LOCAL_BASE["provider"]
        resolved_compute = _LOCAL_BASE["compute_target"]
        resolved_keep_alive = _LOCAL_BASE["keep_alive"]
        resolved_preset = _preset_name_for(raw)
        _role = resolve_role(tool, raw)
        # §3.6 — a binding that no longer resolves is reported against the tool,
        # never silently swapped for something that happens to work.
        resolved_role = _role["role"]
        role_unresolved_reason = None if _role["resolved"] else _role["reason"]

        if _role["resolved"]:
            # §3.5 — in MANAGED local mode the ROLE governs, so the config
            # surface must report the model actually being served and ITS
            # context, not a catalogue default describing a different model.
            # This was the AI Config display mismatch: a reasoning tool on a
            # reasoner-serving tier served gpt-oss-20b at num_ctx 131072 while
            # `local_model`/`num_ctx` reported the catalogue's devstral-small-2
            # at 32768 — a context/VRAM/model-key figure for the WRONG model.
            #
            # Taking the served identity straight from the resolved role also
            # means NOT consulting `resolve_default_model` here, which is what
            # emitted the spurious "No catalogued model can be served" warning
            # while a role model was in fact being served (crying wolf: the
            # catalogue could not place gpt-oss-20b's 22.5 GB floor, but the
            # tier serves it regardless).
            # A stored pin is an operator's ADVANCED-mode choice: remembered and
            # echoed back so that picker still shows it, even though managed mode
            # does not obey it (§3.5). With NO pin, report the model actually
            # SERVED — reporting the catalogue default here (resolve_default_model
            # -> devstral-small-2) beside a served gpt-oss-20b was the display
            # mismatch this fixes.
            resolved_local_model = raw.get("local_model") or _role["served_name"]
            resolved_model = _role["served_name"]
            resolved_base_url = _role["base_url"]
            resolved_num_ctx = _role.get("max_model_len")
            if resolved_num_ctx is None:
                # Defensive: a served model the tier declares without a context
                # still reports one if the catalogue knows it.
                _cat = MODEL_CATALOG.get(_role["served_name"])
                resolved_num_ctx = _cat.get("max_model_len") if _cat else None
        else:
            # No role on this tier — fall back to the catalogue default so the
            # tool still resolves to SOMETHING and the UI can say what. This is
            # the only path that consults the catalogue (and thus the only one
            # that may warn that nothing can be served — correctly, because
            # nothing is).
            entry = _local_model_entry(raw, tool)
            model_key = raw.get("local_model") or resolve_default_model(tool)
            resolved_local_model = model_key
            resolved_model = entry["served_name"]
            resolved_base_url = entry["base_url"]
            # Only devstral has VRAM-tier presets that decide its context;
            # every other catalog model carries its own max_model_len.
            if model_key == "devstral-small-2":
                resolved_num_ctx = _preset_for(raw)["max_model_len"]
            else:
                resolved_num_ctx = entry.get("max_model_len")
    else:
        resolved_provider = raw.get("provider")
        resolved_model = raw.get("model")
        resolved_base_url = raw.get("base_url")
        resolved_compute = raw.get("compute_target") or "cpu"
        resolved_keep_alive = raw.get("keep_alive") or "forever"
        resolved_preset = None
        resolved_num_ctx = None
        # Outside managed mode there is no role — the operator has taken the
        # choice on themselves, which is the point of leaving managed mode.
        resolved_role = None
        role_unresolved_reason = None

    return ToolAIConfig(
        execution_mode=mode,
        provider=resolved_provider,
        model=resolved_model,
        api_key=bool(raw.get("api_key")),
        base_url=resolved_base_url,
        compute_target=resolved_compute,
        keep_alive=resolved_keep_alive,
        temperature=raw.get("temperature", "0.3"),
        max_tokens=raw.get("max_tokens", "4096"),
        local_preset=resolved_preset,
        local_model=resolved_local_model,
        local_role=resolved_role,
        resolved_role_model=resolved_model if resolved_role else None,
        role_unresolved_reason=role_unresolved_reason,
        embedding_model=raw.get("embedding_model") or None,
        embedding_store=raw.get("embedding_store") or None,
        embedding_model_user_rag=raw.get("embedding_model_user_rag") or None,
        embedding_model_docs=raw.get("embedding_model_docs") or None,
        num_ctx=resolved_num_ctx,
        # WIZARD ONLY: surface the code-review setting so the UI can render it.
        # Default "self" when unset (see ToolAIConfig.critic_mode).
        critic_mode=(raw.get("critic_mode") if raw.get("critic_mode") in
                     ("self", "reasoner", "off") else "self"),
        # mesh-native-llm-routing §4.8 — opt-in per-tool LLM fallback.
        fallback_enabled=bool(raw.get("fallback_enabled", False)),
        fallback_tool=raw.get("fallback_tool") or None,
        # GE.AIC.1-3 — echoed back verbatim regardless of mode, same as the
        # embedding pins above, so the Stack picker still shows the saved
        # choice even while another provider is selected.
        stack_id=raw.get("stack_id"),
        served_model=raw.get("served_model") or None,
        embedding_provider=raw.get("embedding_provider") or None,
        embedding_stack_id=raw.get("embedding_stack_id"),
        embedding_provider_user_rag=raw.get("embedding_provider_user_rag") or None,
        embedding_stack_id_user_rag=raw.get("embedding_stack_id_user_rag"),
        embedding_provider_docs=raw.get("embedding_provider_docs") or None,
        embedding_stack_id_docs=raw.get("embedding_stack_id_docs"),
    )


# ---------------------------------------------------------------------------
# GSEC FX.16 R-024 (SEC-14-003) — tenant LLM data routing
# ---------------------------------------------------------------------------
#
# All tenant LLM traffic resolves through the ONE platform-global provider
# binding (the per-tool Vault slot this module owns). Before a tenant request
# is served by an EXTERNAL (non-MOD-hosted) endpoint, the tenant must have
# opted in (platform setting `ai.external_llm_requires_tenant_opt_in`,
# default True; a tenant opts in by setting it to False AT TENANT SCOPE).
# These helpers are the single shared seam the chat surfaces consult — the
# classification is deliberately off the request path (no DNS/TCP probes, so
# a misconfigured or unreachable external host cannot stall a chat turn
# while deciding whether it is external), and the AuditEvent it writes is
# metadata-only (tenant, surface, provider host, byte count) — never content.

_EXTERNAL_LLM_SETTING_KEY = "ai.external_llm_requires_tenant_opt_in"

# Docker compose service aliases / localhost names that MOD serves its own
# managed models on. Anything resolving to these is MOD-hosted, never
# "external". Kept conservative: an unknown private/LAN/mesh host is NOT in
# this set, so a self_hosted endpoint on a worker the operator trusts still
# needs the tenant opt-in only when it is truly external — the mesh MagicDNS
# and tailnet checks below catch MOD's own stacks.
_LOCAL_HOST_ALIASES = {
    "localhost", "0.0.0.0", "127.0.0.1",
    "ollama", "vllm", "vllm-big", "vllm-small", "llm",
    "managed-ollama", "model-cache",
}


def llm_binding_is_external(env: Dict[str, str], tool: Optional[str] = None) -> bool:
    """GSEC R-024 — is a resolved LLM env pointing at an EXTERNAL provider?

    `env` is a resolved `LLM_*` dict (the shape `_resolved_effective_env`
    returns). EXTERNAL means tenant data leaves MOD's infrastructure: a
    cloud provider host (no base_url, or a public hostname). NOT external
    (MOD-hosted / local) means: no binding at all, the managed local mode
    aliases, localhost, RFC1918/CGNAT/link-local addresses, `.local` mDNS
    names, mesh MagicDNS service names, and tailnet (`*.ts.net`) names —
    i.e. anything that stays on MOD's own network.

    Pure string classification: no DNS or TCP probes, so it is safe to call
    on every request and never raises (a malformed URL is treated as
    external — fail closed toward the opt-in gate, never toward bypass).
    """
    base_url = (env.get("LLM_BASE_URL") or "").strip()
    provider = (env.get("LLM_PROVIDER") or "").lower()
    if not base_url:
        # A provider with no endpoint (e.g. `provider=openai` with an
        # api_key but no base_url) dispatches to the provider's own cloud
        # API — that is external by definition.
        return bool(provider) or bool((env.get("LLM_API_KEY") or "").strip())
    host = (urllib.parse.urlparse(base_url).hostname or "").lower().rstrip(".")
    if not host:
        return True
    if host in _LOCAL_HOST_ALIASES:
        return False
    if host.endswith(".local"):
        return False
    # Single-label names (docker service / stack container aliases) only
    # resolve inside MOD's own networks.
    if "." not in host:
        return False
    # MOD's own domain (stack LB endpoints, platform services).
    _base = (os.getenv("BASE_DOMAIN") or "").lower().strip(".")
    if _base and (host == _base or host.endswith("." + _base)):
        return False
    # Mesh MagicDNS service names (stacks on the overlay) and tailnet
    # nodes: MOD's own infrastructure, even if the name is not a plain IP.
    if ".ts.net" in host or host.endswith(".svc.cluster.local"):
        return False
    # IPv4 literal — classify by range.
    if host.count(".") == 3 and all(p.isdigit() for p in host.split(".")):
        a, b = (int(p) for p in host.split(".")[:2])
        if a in (10, 0) or a == 172 and 16 <= b <= 31 or a == 192 and b == 168 \
                or a == 169 and b == 254:
            return False
        return True
    # IPv6 literal — private/link-local are MOD-local.
    if ":" in host:
        v6 = host.strip("[]")
        return not (v6.startswith("fe80") or v6.startswith("fc")
                    or v6.startswith("fd") or v6 == "::1")
    # Anything else (a public hostname) is external.
    return True


def resolve_llm_data_routing(db: Session, tenant_id: Optional[int],
                             env: Dict[str, str], surface: str,
                             tool: Optional[str] = None,
                             byte_count: Optional[int] = None,
                             workflow_id: Optional[int] = None,
                             run_id: Optional[int] = None,
                             file_ids: Optional[tuple] = None) -> Dict[str, Any]:
    """GSEC R-024 — the shared decision point for tenant LLM data routing.

    Returns a dict:
      allowed   bool — the request may be served by this binding.
      external  bool — the binding is external (see
                      `llm_binding_is_external`).
      setting   str — the platform-setting key that governs the gate.
      message   Optional[str] — the refusal text (names the setting) when
                not allowed.

    Behaviour:
      - No configured binding (`env` lacks a base_url/model) -> allowed
        (there is nothing to route; the caller's own not-configured
        handling applies).
      - MOD-hosted / local binding -> always allowed.
      - External binding + gate ON (default) -> allowed only when the
        tenant has a tenant-scope row for the setting set to False
        (explicit opt-in).
      - External binding + gate OFF (platform default set False) ->
        allowed for all tenants.

    The outcome is recorded as a metadata-only `AuditEvent`
    (`llm_external_routing` / `llm_local_routing`) — tenant, surface,
    provider host, byte count, allowed. NEVER the prompt or log content.
    Never raises: a routing-decision helper must not take down the caller's
    own request or its failure reporting.
    """
    external = False
    host = ""
    try:
        external = llm_binding_is_external(env, tool)
        host = (urllib.parse.urlparse(
            (env.get("LLM_BASE_URL") or "")).hostname or "").lower()
    except Exception:  # noqa: BLE001 — classification must not raise
        external, host = True, ""
    setting = _EXTERNAL_LLM_SETTING_KEY
    # GSEC FX.19 R-042 — the platform-wide kill switch. When
    # `egress.external_llm_enabled` is False (or `egress.sealed_mode` is
    # True) EXTERNAL bindings are refused for every tenant regardless of
    # opt-in; local / MOD-hosted bindings are unaffected. The R-024
    # per-tenant opt-in gate below still applies when this switch allows.
    if external:
        from utils import egress_policy
        if not egress_policy.egress_allowed(egress_policy.CATEGORY_EXTERNAL_LLM,
                                            db=db):
            logger.warning(
                "R-042: external LLM binding refused — egress disabled "
                "(surface=%s, host=%s)", surface, host)
            ev = models.AuditEvent(
                tenant_id=tenant_id,
                event_name="llm_external_routing",
                details={
                    "surface": surface, "tool": tool or "assistant",
                    "provider_host": host or None, "external": True,
                    "allowed": False, "gate_on": False,
                    "tenant_opted_in": False,
                    "setting": "egress.external_llm_enabled",
                    "byte_count": byte_count, "reason": "egress_disabled",
                },
            )
            db.add(ev)
            db.flush()
            return {"allowed": False, "external": True,
                    "setting": "egress.external_llm_enabled",
                    "message": "External LLM egress is disabled on this "
                               "platform (egress.external_llm_enabled, "
                               "or egress.sealed_mode). Tenant data is not "
                               "sent to external model endpoints. Point the "
                               "AI binding at a MOD-hosted (local/stack) "
                               "endpoint, or re-enable the platform "
                               "setting. No fallback model was used.",
                    "provider_host": host}
    # GY.C7 — restricted-LABELLED data never goes to external LLMs. An
    # asset/workflow/run carrying a restricted classification (restricted /
    # CUI / EAR / ITAR) blocks the external binding regardless of the
    # tenant's R-024 opt-in. Local / MOD-hosted bindings are unaffected
    # (this branch is external-only). A settings/lookup read failure blocks
    # (fail closed — see utils/label_policy).
    if external:
        from utils import label_policy
        label_reason = label_policy.external_llm_blocked_for(
            db, tenant_id=tenant_id, workflow_id=workflow_id,
            run_id=run_id, file_ids=file_ids or ())
        if label_reason:
            logger.warning(
                "GY.C7: external LLM binding refused — restricted-labelled "
                "data (surface=%s, host=%s)", surface, host)
            try:
                db.add(models.AuditEvent(
                    tenant_id=tenant_id,
                    event_name=label_policy.BLOCK_AUDIT_EVENT,
                    details={
                        "surface": surface,
                        "tool": tool or "assistant",
                        "provider_host": host or None,
                        "external": True,
                        "workflow_id": workflow_id,
                        "run_id": run_id,
                        "file_ids": list(file_ids) if file_ids else [],
                        "setting": label_policy.BLOCK_SETTING_KEY,
                        "reason": label_reason,
                    },
                ))
                db.flush()
            except Exception:  # noqa: BLE001 — audit must not block refusal
                logger.warning("GY.C7: failed to record %s audit event",
                               label_policy.BLOCK_AUDIT_EVENT, exc_info=True)
            return {"allowed": False, "external": True,
                    "setting": label_policy.BLOCK_SETTING_KEY,
                    "message": label_reason,
                    "provider_host": host}
    if external:
        try:
            from services import platform_settings as psr
            platform_on = psr.get_setting(setting, db=db) is True
            tenant_value = (psr.get_setting(setting, db=db, tenant_id=tenant_id)
                            if tenant_id is not None else None)
            opted_in = (tenant_value is False)
            allowed = (not platform_on) or opted_in
        except Exception:  # noqa: BLE001 — fail closed on gate-read errors
            platform_on, opted_in, allowed = True, False, False
        message = None if allowed else (
            "This tenant's AI configuration routes assistant/support chat "
            f"to an external model endpoint ({host or 'a cloud provider'}). "
            "Tenant data is not sent there unless the tenant has explicitly "
            f"opted in: set the platform setting '{setting}' to false for "
            "this tenant, or point the AI binding at a MOD-hosted (local/"
            "stack) endpoint. No fallback model was used."
        )
    else:
        platform_on, opted_in, allowed = False, False, True
        message = None
    try:
        ev = models.AuditEvent(
            tenant_id=tenant_id,
            event_name=("llm_external_routing" if external
                        else "llm_local_routing"),
            details={
                "surface": surface,
                "tool": tool or "assistant",
                "provider_host": host or None,
                "external": external,
                "allowed": allowed,
                "gate_on": platform_on,
                "tenant_opted_in": opted_in,
                "setting": setting,
                "byte_count": byte_count,
            },
        )
        db.add(ev)
        db.flush()
    except Exception:  # noqa: BLE001 — audit logging must not block serving
        logger.warning("R-024: failed to record LLM routing audit event",
                       exc_info=True)
    return {"allowed": bool(allowed), "external": external,
            "setting": setting, "message": message, "provider_host": host}


def _resolved_effective_env(tool: str) -> Dict[str, str]:
    """ai-config-stack-binding 2.1 — the `{}` half of the two entry points.

    A thin wrapper over :func:`services.ai_slot_resolver.resolve_slot`: the
    ``LLM_*`` env when the slot resolves ``ready``, and ``{}`` for EVERY other
    outcome (``no_binding | stack_stopped | stack_missing | unreachable |
    waking``). The dict can never carry a diagnostic key without leaking one
    into a real request or task container (the failure mode
    :func:`_stack_provider_error` documents), so the reason lives in the
    resolver and every consumer that REPORTS to a human calls
    :func:`services.ai_slot_resolver.resolve_slot` directly instead.

    Transitional: while a slot has no binding row at all (nobody has picked
    through the new Settings -> AI surface, or the 1.2 alias migration has not
    run), it still answers from its own stored config — the same slot, never
    another model and never another slot (Q-AI1 = B). Once a row exists, that
    row is the truth and a failure returns ``{}`` here with its cause in the
    resolver.
    """
    from services.ai_slot_resolver import resolve_slot
    # The resolver needs a session to see the binding rows at all — the legacy
    # path below opens one the same way (`_legacy_effective_env`'s provider=='stack'
    # branch), so this wrapper changes nothing about how the DB is reached.
    db = None
    try:
        from database import SessionLocal
        db = SessionLocal()
    except Exception:  # noqa: BLE001 — no DB in play (unit contexts)
        db = None
    try:
        res = resolve_slot(db, tool)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
    if res["outcome"] == "ready":
        return res["env"]
    if res["outcome"] != "no_binding":
        # A binding exists and did not serve: `{}` + the named cause in the
        # resolver (the spread sites' existing "not configured" handling fires).
        logger.info("ai-config slot unresolved for tool=%s: %s", tool,
                    res["message"])
        return {}
    return _legacy_effective_env(tool)


def _legacy_effective_env(tool: str) -> Dict[str, str]:
    """Resolve the tool's config into the LLM_* env vars a task container needs.

    For Local mode, derives provider/model/base_url from the selected
    preset. vLLM exposes OpenAI-compat at /v1/*, so Local mode uses
    provider=openai_compatible and the preset's served_name as model.
    No LLM_NUM_CTX or LLM_KEEP_ALIVE — vLLM ignores those (context is
    fixed at container start via --max-model-len; residency is "always
    loaded" by nature of the server).

    For other modes, reads stored Vault values unchanged. Self-hosted
    Ollama + Advanced modes may still send keep_alive/num_ctx because
    the tenant's own Ollama might accept them.
    """
    raw = _read_tool_config(tool)
    mode = raw.get("execution_mode", "local")
    # GE.AIC.1-3 — provider == "stack" resolves through the running
    # PersistentStack instead of the persisted base_url/model. This is the
    # ONE chokepoint (utils/llm_client.py, services/assistant_orchestrator.py,
    # services/support_chat_shared_service.py all call `_resolved_effective_env`
    # for their tool's binding), so handling it here covers every chat slot.
    # A stopped/missing stack raises — no silent fallback to another model
    # unless the tool's OWN fallback_enabled/fallback_tool is on, which the
    # callers above already handle by trying `fallback_tool`'s env next.
    if raw.get("provider") == "stack":
        from services.ai_stack_provider import resolve_stack_endpoint, StackProviderError
        own_session = False
        db = None
        try:
            from database import SessionLocal
            db = SessionLocal()
            own_session = True
            base_url, model_name = resolve_stack_endpoint(
                db, raw.get("stack_id"), raw.get("served_model"))
        except StackProviderError as e:
            # §"no silent fallback to another model" — this must not degrade
            # to a DIFFERENT model. It degrades to UNCONFIGURED (empty env),
            # the exact state every other misconfigured slot already returns
            # here, so each consumer's existing "not configured" handling
            # (llm_client's LLMClientError / the assistant's canned-response
            # path) fires instead of an unhandled crash mid-request. The
            # specific reason naming the stack is logged for an operator to
            # find, and is exactly what `resolve_stack_endpoint` raises for a
            # direct caller (e.g. a save-time validation check).
            logger.warning("ai-config stack provider unresolved for tool=%s: %s",
                            tool, e)
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
        entry = _local_model_entry(raw, tool)
        # §2.2 — in managed local mode the ROLE decides, and the active tier
        # supplies the model. Without this the resolver existed and was tested
        # while the serving path still read MODEL_CATALOG, so a tier change
        # re-pointed nothing — the exact manual step role binding removes. It
        # also sent reasoning tools to the GENERATOR's endpoint, because that is
        # the catalogue default.
        #
        # An explicit `local_model` pin still wins: that is an operator
        # overriding their own tier deliberately.
        # §3.5 — in MANAGED mode the role governs, full stop. A `local_model`
        # left over from advanced mode stops applying the moment a tool returns
        # here; that is what "managed" means, and honouring a stale pin would
        # reintroduce the per-tool drift role binding exists to remove. An
        # operator who wants a specific model uses advanced mode, where the
        # choice is theirs and is stated as such.
        #
        # The catalogue entry is the FALLBACK, used only when the active tier
        # serves no such role — otherwise a tool would resolve to nothing at all.
        role = resolve_role(tool, raw)
        if role["resolved"]:
            served, base = role["served_name"], role["base_url"]
        else:
            # CE (owner 11-10 item 46) - a CE box with no resolved role must
            # read "not configured". This branch is only reached with no
            # binding row (the wrapper returns `{}` for every other resolver
            # outcome), and here `role` did not resolve (on CE
            # `services.managed_ai_stacks` is denied, so `tier_roles()` is
            # empty). The catalogue fallback below would hand every tool the
            # managed-local default host (`_LOCAL_BASE["base_url"]` =
            # http://ollama:11434), which the CE compose never starts, so the
            # wizard chip would read "Can't reach LLM: host='ollama'". That
            # holds even when managed Local mode was saved (`raw` carries only
            # execution_mode local), so any unresolved CE role is "not
            # configured". A CE user who bound a model keeps working (the row
            # resolves in the wrapper, outcome "ready"); advanced/BYO/stack
            # config takes the branches above. Non-CE editions are unchanged.
            from utils.paid_import import is_ce_edition
            if is_ce_edition():
                return {}
            served, base = entry["served_name"], entry["base_url"]
        env = {
            "LLM_PROVIDER": _LOCAL_BASE["provider"],
            "LLM_MODEL": served,
            "LLM_BASE_URL": base,
            # The real key when one is configured. When there is no key,
            # send an empty string so no Authorization header is sent.
            # The sentinel `sk-vllm-no-auth` was harmless while the port was
            # loopback-bound, but now the port is published and the sentinel
            # key would authorize anyone who can reach the worker.
            "LLM_API_KEY": _managed_key(role=role.get("role")),
            "LLM_TEMPERATURE": raw.get("temperature", "0.3"),
            "LLM_MAX_TOKENS": raw.get("max_tokens", "4096"),
            "LLM_REASONING": "1" if entry.get("reasoning") else "",
        }
        return {k: v for k, v in env.items() if v}

    # Self-hosted / cloud / advanced: honour persisted values + Ollama-
    # specific knobs for tenants running their own Ollama box.
    keep_alive = raw.get("keep_alive") or "forever"
    keep_alive_wire = {
        "off": "0", "5m": "5m", "30m": "30m", "1h": "1h", "forever": "-1",
    }.get(keep_alive, "-1")

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


def _stack_provider_error(tool: str) -> Optional[str]:
    """GE.AIC.2 follow-up (2026-09-29) — why a `provider == "stack"` slot's
    `_resolved_effective_env(tool)` came back empty.

    `_resolved_effective_env` MUST keep degrading a failed stack resolution
    to `{}` (an empty dict) on failure — every existing caller (llm_client,
    assistant_orchestrator, the many `**_resolved_effective_env(...)` spreads
    building a literal container env) treats that dict as the actual
    LLM_* env to use/spread, so it can never carry a diagnostic key without
    leaking one into a real request or task container.

    This is the side-channel a caller that already found "nothing
    configured" can consult to tell a human WHY, before falling back to a
    generic "not configured" message: a stopped/missing/unreachable stack
    is a materially different, actionable situation (start the stack) from
    "nobody ever set this slot up".

    Returns `None` when the slot isn't `provider == "stack"`, or resolution
    currently succeeds — i.e. whenever the generic not-configured message is
    still the right one. Never raises: a diagnostic helper must not itself
    take down the caller's own failure-reporting path.
    """
    try:
        raw = _read_tool_config(tool)
    except Exception:  # noqa: BLE001
        return None
    if raw.get("provider") != "stack":
        return None
    from services.ai_stack_provider import resolve_stack_endpoint, StackProviderError
    db = None
    try:
        from database import SessionLocal
        db = SessionLocal()
        resolve_stack_endpoint(db, raw.get("stack_id"), raw.get("served_model"))
        return None  # resolves fine right now — not the empty-env cause
    except StackProviderError as e:
        # str(e) already names the stack, e.g. "Stack 'my-stack' (#12) is
        # stopped, not running — start it before it can serve this slot." or
        # "Stack #12 was not found." — exactly the "stopped/missing" shape.
        return str(e)
    except Exception:  # noqa: BLE001
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


# =============================================================================
# Managed LLM health — drives UI gating of the Local option
# =============================================================================

# Weight-file sizes, keyed by "repo:quant". Looked up once from HuggingFace and
# kept for the process lifetime: a published GGUF does not change size, and the
# alternative is an outbound HTTP call on every render of the settings page.
_WEIGHT_SIZE_CACHE: Dict[str, Optional[int]] = {}


def _weight_total_bytes(model_ref: str) -> Optional[int]:
    """Total size of the GGUF a model reference names, or None.

    `model_ref` is what the stack spec records, e.g.
    "ibm-granite/granite-3.3-2b-instruct-GGUF:Q4_K_M".

    None on any failure, and the caller shows bytes-so-far without a
    percentage. A progress bar that invented its own denominator would be worse
    than no bar: it would move smoothly and mean nothing.
    """
    if not model_ref or ":" not in model_ref:
        return None
    if model_ref in _WEIGHT_SIZE_CACHE:
        return _WEIGHT_SIZE_CACHE[model_ref]

    # GSEC FX.19 R-042 — never make the HF size-probe HEAD request when
    # model downloads are disabled (sealed mode or egress.model_download_
    # enabled False); the caller shows bytes-so-far without a percentage.
    from utils import egress_policy
    if not egress_policy.egress_allowed(egress_policy.CATEGORY_MODEL_DOWNLOAD):
        return None

    repo, quant = model_ref.rsplit(":", 1)
    total: Optional[int] = None
    try:
        import httpx
        # HF's GGUF convention: <basename>-<QUANT>.gguf at the repo root.
        base = repo.split("/")[-1].removesuffix("-GGUF")
        url = f"https://huggingface.co/{repo}/resolve/main/{base}-{quant}.gguf"
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            resp = client.head(url)
        if resp.status_code == 200:
            cl = resp.headers.get("content-length")
            total = int(cl) if cl and cl.isdigit() else None
    except Exception:  # noqa: BLE001
        logger.debug("weight size lookup failed for %s", model_ref, exc_info=True)

    # Negative results are cached too — a deployment with no egress must not
    # retry this on every page load.
    _WEIGHT_SIZE_CACHE[model_ref] = total
    return total


# managed_ai_wanted_models / managed_ai_download_progress: policy moved to
# services/ai_config_service.py (Gate 2b — the db.query()s that used to live
# inline here now go through crud/ai_config.py). Re-exported under their
# original names so every existing importer
# (`from routers.ai_config import managed_ai_wanted_models`, etc.) is
# unaffected.
from services.ai_config_service import (  # noqa: E402,F401
    managed_ai_wanted_models,
    managed_ai_download_progress,
)


class ManagedLLMHealthResponse(BaseModel):
    healthy: bool
    # 8.3b — None means "no endpoint known": `_role_base_url` no longer invents
    # an alias URL when nothing is derivable, so the probe reports no address
    # rather than a black hole.
    base_url: Optional[str] = None
    # List of --served-model-name values the vLLM server is currently
    # serving. One entry per loaded model (vLLM today is one-per-server).
    served_models: list[str]
    # Which preset the admin last saved via the Settings UI. If this
    # differs from the running served_models, the UI surfaces a
    # "restart required" badge.
    saved_preset: Optional[str] = None
    running_matches_saved: bool = True
    # Which path serves managed AI here. On the STACK path the API redeploys
    # the models itself, so a mismatch means the redeploy is still running or
    # it failed — never "go and run docker compose", which is the compose
    # path's remedy and is simply wrong advice here.
    as_stacks: bool = False
    # In-flight weight downloads with a denominator. Empty means nothing is
    # downloading; the UI uses that to remove a progress bar rather than
    # leaving a stale one on screen.
    downloads: List[Dict[str, Any]] = []
    error: Optional[str] = None


def _serving_matches(saved_served_name: Optional[str],
                     served_models: List[str]) -> bool:
    """Is the saved preset's model the one actually being served?

    This used to be `saved_served_name in served_models` — an exact
    list-membership test. LOCAL_PRESETS carries a SHORT label
    ("granite-3.3-8b") while a server reports its full identifier
    ("ibm-granite/granite-3.3-8b-instruct-GGUF:Q4_K_M"), so the test could
    never pass on the GGUF rungs. The page told every operator on
    `compact` to restart a server that was already serving exactly the
    right model, and gave them a compose command to do it.

    A banner that cries wolf is worse than no banner: the one time it is
    right, it has already been trained away.

    Matched by normalised containment in either direction — the short
    label inside the full id, or a served id inside a longer configured
    name. Deliberately loose: the cost of a missed warning is one manual
    restart, while the cost of a false one is an operator who stops
    reading warnings.
    """
    if not saved_served_name:
        return True
    want = saved_served_name.strip().lower()
    if not want:
        return True
    for served in served_models:
        got = (served or "").strip().lower()
        if not got:
            continue
        if want == got or want in got or got in want:
            return True
    return False


@router.get("/llm-health", response_model=ManagedLLMHealthResponse)
def get_managed_llm_health(
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    """Ping the compose-level vLLM server's /v1/models endpoint.

    Dashboard uses this to:
    - Grey out the Local option when vLLM isn't running (no GPU or the
      admin skipped the `vllm` profile at boot).
    - Show a "pending restart" badge when the admin has saved a preset
      that doesn't match what vLLM is currently serving.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    from services.ai_config_service import build_managed_llm_health
    return build_managed_llm_health(db)


# =============================================================================
# Per-tool endpoints
# =============================================================================

@router.get("/ai-config/model-catalog")
def get_model_catalog(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """List the local models a tool can be routed at. Drives the per-tool
    model picker in Settings. `available=False` = catalogued but its
    serving endpoint isn't stood up yet. No secrets here — safe to show."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    from services.ai_config_service import build_model_catalog
    return build_model_catalog(db)


_SEEN_PLACEABLE_PATH = "platform/ai-config/_seen_placeable_rungs"

# _annotate_newly_available: policy moved to services/ai_config_service.py
# (Gate 2b). Re-exported under its original name — direct-call unit tests
# (`ai_config._annotate_newly_available(...)`) and `ai_config._SEEN_PLACEABLE_PATH`
# (read back by the service via a lazy import) are both unaffected.
from services.ai_config_service import _annotate_newly_available  # noqa: E402,F401


@router.get("/ai-config/tiers")
def get_tier_ladder(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """The full preset ladder, with what each rung can actually serve.

    managed-ai-surfaces-and-stack-ops §6.1. Every value here comes from
    `tier_capabilities()`, which already does this arithmetic — including the
    wizard's own context-budget calculation rather than a copy of it. The
    frontend must render this and compute none of it.

    That matters because the Dashboard previously kept a hand-maintained mirror
    of LOCAL_PRESETS in JavaScript, listing three rungs with their VRAM figures
    typed in by hand. It went stale the moment `cpu` and `minimal` were added:
    users could not select two tiers that existed and worked, and the numbers
    shown for the other three were only as current as the last person to
    remember to edit both files.

    `ladder` is ordered cheapest-first, so the UI can render it as a ladder
    without knowing the order itself.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    from services.ai_config_service import build_tier_ladder
    return build_tier_ladder(db)


@router.get("/managed-ai/setting", response_model=ManagedAISettingResolved)
def get_managed_ai_setting(
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """§1.6 — read the effective managed-AI tier + accelerator kind.

    Tenant-admin gated, matching every other managed-AI endpoint in this
    surface (`/ai-config/tiers`, `/llm-health`, …).
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    return resolve_managed_ai_setting()


@router.put("/managed-ai/setting", response_model=ManagedAISettingResolved)
def put_managed_ai_setting(
    body: ManagedAISetting,
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """§1.6 — persist the tier and/or accelerator kind an operator picked.

    This is what makes §1.3 real: without a write path the resolver would
    forever report "env" or "default" and never "stored", because nothing
    could put a value where it looks first. Both fields optional — either can
    be set alone, matching every other partial-update endpoint in this file.

    Validated against the same tables the resolver falls back to when a value
    is unrecognised (§1.5), so a typo is refused here rather than accepted and
    silently overridden on the next read — a confusing way to fail, since the
    write would appear to succeed and then never take effect.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")

    if body.tier is not None:
        tier = body.tier.strip()
        if tier not in LOCAL_PRESETS:
            raise HTTPException(
                status_code=422,
                detail=f"unknown tier {tier!r}; choose one of {sorted(LOCAL_PRESETS)}")

    kind = None
    if body.accelerator_kind is not None:
        # CE guard (GT.D4b pattern, precedent routers/api_keys.py:281 one-line
        # optional_paid): services.managed_ai_stacks is paid and absent on CE.
        # On CE the kind validation block is skipped — kind stays None, the
        # stored write below is unchanged, and the 422 for an unknown kind is
        # raised ONLY when the module is present (paid behaviour identical).
        from utils.paid_import import optional_paid
        _mai = optional_paid("services.managed_ai_stacks")
        if _mai is not None:
            kind = _mai.normalize_kind(body.accelerator_kind)
            known = {k["kind"] for k in _mai.accelerator_kinds()}
            if kind not in known:
                raise HTTPException(
                    status_code=422,
                    detail=f"unknown accelerator kind {kind!r}; choose one of {sorted(known)}")

    stored = _read_vault_path(_MANAGED_AI_SETTING_PATH) or {}
    if body.tier is not None:
        stored["tier"] = body.tier.strip()
    if kind is not None:
        stored["accelerator_kind"] = kind
    _write_vault_path(_MANAGED_AI_SETTING_PATH, stored)
    _invalidate_managed_ai_setting_cache()
    return resolve_managed_ai_setting(force=True)


# fleet_accelerator_kind / _tier_with_placement: policy moved to
# services/ai_config_service.py (Gate 2b — the db.query()s that used to live
# inline here now go through crud/ai_config.py). Re-exported under their
# original names: routers/persistent_stacks/_shared.py lazy-imports
# `fleet_accelerator_kind` from this module, and several tests import +
# directly call both by name.
from services.ai_config_service import (  # noqa: E402,F401
    fleet_accelerator_kind,
    _tier_with_placement,
)


# ---------------------------------------------------------------------------
# managed-ai-self-deploy §3 — the plan, computed by the code that will
# execute it.
# ---------------------------------------------------------------------------

def _served_model_ref(compose_spec: Optional[dict]) -> Optional[str]:
    """The model reference a compose spec serves, read off `x-mod.served.model`
    — the same field `managed_ai_wanted_models` reads. First service that
    declares one; the generator and reasoner specs each declare exactly one
    service, so there is never a second to disagree with the first."""
    from services.stack_model_endpoint import served_model_ref
    return served_model_ref(compose_spec)


class ManagedAIPlanAction(BaseModel):
    stack: str
    role: Optional[str] = None
    action: Literal["deploy", "stop", "leave"]
    reason: str
    current_model: Optional[str] = None
    target_model: Optional[str] = None


class ManagedAIDownloadEstimate(BaseModel):
    # §3.4 — an upper bound, not a promise. The API cannot see what a worker's
    # local weight cache already holds, so a stack that would "deploy" may in
    # fact start in seconds because the weights are already there.
    upper_bound_bytes: int = 0
    # True when at least one deploying stack's weight size could not be looked
    # up (no egress, or the repo/quant naming convention didn't resolve) — the
    # real download may be LARGER than upper_bound_bytes, never smaller.
    unknown: bool = False
    note: str


class ManagedAIPlan(BaseModel):
    tier: str
    accelerator_kind: Optional[str] = None
    actions: List[ManagedAIPlanAction]
    # §3.2 — true only when every declared stack is already deployed with a
    # matching compose spec. Identity alone (the stack exists under this role)
    # would silently skip a model change within the same tier.
    already_correct: bool
    download_estimate: ManagedAIDownloadEstimate
    # The verdict from the SAME accelerator-fit path `/ai-config/tiers` uses
    # (§3.5) — placeable, the shortfall when not, and a tier that would fit.
    placement: Optional[Dict[str, Any]] = None
    stack_path_enabled: bool
    message: str


# compute_managed_ai_plan: policy moved to services/ai_config_service.py
# (Gate 2b — the db.query() that used to live inline here now goes through
# crud/ai_config.py). Re-exported under its original name for symmetry with
# the other helpers in this section (no external importer found by name, but
# `get_managed_ai_plan` below calls it as a bare name).
from services.ai_config_service import compute_managed_ai_plan  # noqa: E402,F401


@router.get("/managed-ai/plan", response_model=ManagedAIPlan)
def get_managed_ai_plan(
    preset: str,
    kind: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """§3.6 — preview what applying `preset` would do, without doing it.

    §3.3 — dry-runs the exact diff Apply executes (same source functions, same
    stack-name + compose-spec comparison) so a preview can never promise
    something Apply then does differently.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    return compute_managed_ai_plan(db, preset, kind)


class ToolDerivation(BaseModel):
    """What one tool would resolve to on a candidate tier — the role it binds
    to, the model that role serves, where it answers, and whether the tier
    serves that role at all. `reason` is populated only when it does not."""
    tool: str
    role: str
    # Always "local": System managed runs every tool as managed local. Named
    # explicitly because §4.1 derives each tool's model AND its execution mode,
    # and leaving it implicit is how a reader assumes the previous mode carried.
    execution_mode: str
    model: Optional[str] = None
    served_name: Optional[str] = None
    base_url: Optional[str] = None
    resolved: bool
    reason: Optional[str] = None
    # §6.3 — this binding comes from the VRAM budget (the sizing dropdown), as
    # opposed to the platform defaults below. Tagged so a poor result is
    # attributed to the right mechanism when inspecting.
    source: str = "budget_derived"


# §6.1/§6.4 — the non-sizing settings System managed applies on the operator's
# behalf, deliberately conservative and revisable by the subsystem owners in
# THIS one place. FIFO already governs scheduling whenever ADVANCED_SCHEDULING
# is not entitled (open-core-tier-structure §2), and training/agent sit at their
# off/conservative baseline until Advanced turns them on — so these are the
# governing values, stated here rather than left implicit.
_MANAGED_NON_SIZING_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "scheduling": {
        "policy": "fifo",
        "note": ("First-in-first-out. Learning/batch/priority scheduling is a "
                 "paid capability (ADVANCED_SCHEDULING) and stays off here."),
    },
    "training": {
        "enabled": False,
        "note": "Model training/fine-tuning is off; enable it in Advanced if needed.",
    },
    "agent": {
        "autonomy": "conservative",
        "note": "The agent runs with conservative defaults; Advanced exposes the full controls.",
    },
}


def managed_ai_platform_defaults() -> Dict[str, Any]:
    """§6.1/§6.3 — the non-sizing settings System managed applies (Scheduler,
    Training, Agent, ...), each tagged `platform_default` so it is never mistaken
    for a budget-derived value: a poor scheduler default must be blamed on the
    platform opinion, not the VRAM dropdown. Conservative, and revisable in one
    place by the subsystem owners (§6.4)."""
    return {k: {**v, "source": "platform_default"}
            for k, v in _MANAGED_NON_SIZING_DEFAULTS.items()}


class ManagedAIDerived(BaseModel):
    """§4.6/§7.5 — the per-tool configuration a tier derives, inspectable
    WITHOUT applying it.

    The tool-level companion to `/managed-ai/plan`: the plan says which stacks
    deploy, this says what each of the five tools binds to and why — so an
    operator can see what System managed decided on their behalf rather than
    trusting a dropdown, and the one who wants to know can read the derivation
    the mode otherwise hides.
    """
    tier: str
    accelerator_kind: str
    # True only when every tool resolved. A tier that serves no reasoner leaves
    # the reasoning tools unresolved WITH a reason — surfaced, never hidden.
    fully_resolved: bool
    tools: Dict[str, ToolDerivation]
    # §6.1/§6.3 — the settings the budget does NOT determine, applied as platform
    # defaults and kept distinguishable from the budget-derived tool bindings
    # above (each entry is tagged source="platform_default").
    platform_defaults: Dict[str, Any] = {}


def compute_managed_ai_derived(target: Optional[str] = None,
                               kind: Optional[str] = None) -> ManagedAIDerived:
    """Project `resolve_role` across all five tools for a candidate tier.

    Side-effect-free: reads nothing an operator has stored and writes nothing.
    Uses the SAME `resolve_role` the live per-tool path uses, so what this
    previews is exactly what a tool binds to once the tier is applied — the
    derivation itself, not a parallel description that could drift from it.

    Binds each tool to its DEFAULT role (not whatever a prior advanced session
    may have pinned): this is a preview of what System managed would derive,
    and System managed owns the role binding.
    """
    resolved = resolve_managed_ai_setting()
    tier = target or resolved.tier
    if tier not in LOCAL_PRESETS:
        raise HTTPException(status_code=422, detail=f"unknown tier {tier!r}")
    acc_kind = kind or resolved.accelerator_kind
    tools: Dict[str, ToolDerivation] = {}
    all_ok = True
    for tool in TOOLS:
        r = resolve_role(tool, {"local_preset": tier})
        if not r.get("resolved"):
            all_ok = False
        tools[tool] = ToolDerivation(
            tool=tool, role=r["role"], execution_mode="local",
            model=r.get("model"), served_name=r.get("served_name"),
            base_url=r.get("base_url"), resolved=bool(r.get("resolved")),
            reason=r.get("reason"),
        )
    return ManagedAIDerived(tier=tier, accelerator_kind=acc_kind,
                            fully_resolved=all_ok, tools=tools,
                            platform_defaults=managed_ai_platform_defaults())


@router.get("/managed-ai/derived", response_model=ManagedAIDerived)
def get_managed_ai_derived(
    preset: Optional[str] = None,
    kind: Optional[str] = None,
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """§4.6/§7.5 — show what each tool would derive on `preset` (default: the
    effective tier), and why, without changing anything. The inspectable view
    for the operator who does want to know what the mode decided."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    return compute_managed_ai_derived(preset, kind)


# ---------------------------------------------------------------------------
# system-managed-ai §3.2 — retain the operator's Advanced configuration across
# a mode switch and restore it on return, DURABLY (not just for the browser
# session). Nobody with a working setup will try System managed if entering it
# destroys the configuration they built, so the prior per-tool config is
# snapshotted to Vault on entry and written back on return.
# ---------------------------------------------------------------------------
_ADVANCED_STASH_PATH = "platform/ai-config/_advanced_stash"


class AdvancedStashResult(BaseModel):
    """The outcome of a stash/restore. `tools` names the tool configs captured
    or written back; `changed` is false when there was nothing to do (a stash
    already held, or nothing was stashed)."""
    action: Literal["stash", "restore"]
    changed: bool
    tools: List[str]
    message: str


@router.post("/managed-ai/advanced-config/stash", response_model=AdvancedStashResult)
def stash_advanced_config(
    force: bool = False,
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """§3.2 — snapshot the current per-tool AI configuration before System
    managed overwrites it.

    Call this on entering System managed, BEFORE switching the tools to managed
    local. Idempotent by default: an existing stash is NOT overwritten, because
    the first snapshot is the genuine Advanced config and a second call once the
    tools are already local would capture the managed state instead — destroying
    exactly what the stash exists to protect. `force=true` overwrites
    deliberately.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    existing = (_read_vault_path(_ADVANCED_STASH_PATH) or {}).get("tools") or {}
    if existing and not force:
        return AdvancedStashResult(
            action="stash", changed=False, tools=sorted(existing),
            message="A prior configuration is already stashed; kept it.")
    snapshot = {tool: _read_tool_config(tool) for tool in TOOLS}
    _write_vault_path(_ADVANCED_STASH_PATH, {"tools": snapshot})
    return AdvancedStashResult(
        action="stash", changed=True, tools=list(TOOLS),
        message="Advanced configuration stashed.")


@router.post("/managed-ai/advanced-config/restore", response_model=AdvancedStashResult)
def restore_advanced_config(
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """§3.2 — restore the configuration stashed on entering System managed, and
    clear the stash. A no-op (`changed=false`) when nothing was stashed, so
    calling it whenever the operator returns to Advanced is always safe."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    stashed = (_read_vault_path(_ADVANCED_STASH_PATH) or {}).get("tools") or {}
    if not stashed:
        return AdvancedStashResult(
            action="restore", changed=False, tools=[],
            message="Nothing was stashed; nothing to restore.")
    written = []
    for tool in TOOLS:
        cfg = stashed.get(tool)
        if cfg is not None:
            _write_vault_path(_vault_path_for(tool), cfg)
            written.append(tool)
    # Clear the stash so the next entry captures a fresh snapshot rather than a
    # stale one from a mode switch two changes ago.
    _write_vault_path(_ADVANCED_STASH_PATH, {})
    # Residency follows the restored configs — never leave the managed Ollama
    # pinned to a preset the restored Advanced config no longer uses. Best-effort:
    # a reconcile hiccup must not fail a restore that already wrote the configs.
    try:
        should = _any_tool_in_local_mode()
        _reconcile_managed_ollama_residency(
            should, _active_preset_for_managed_ollama() if should else None)
    except Exception:  # noqa: BLE001
        logger.debug("residency reconcile after restore failed", exc_info=True)
    return AdvancedStashResult(
        action="restore", changed=True, tools=written,
        message="Advanced configuration restored.")


@router.get("/ai-config/all", response_model=Dict[str, ToolAIConfig])
def get_all_ai_configs(
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Return every configurable tool's config in one shot (the tier-managed
    tools plus the persistable pseudo-tools, e.g. the separate-reviewer
    "critic"). Used by the dashboard on load."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    return {tool: _resolve_config(tool, _read_tool_config(tool))
            for tool in CONFIGURABLE_TOOLS}


class StackOption(BaseModel):
    """One PersistentStack row, projected for the AI-Config Stack picker."""
    stack_id: int
    name: str
    status: str
    kind: Literal["chat", "embedding"]
    served_models: List[str]


@router.get("/ai-config/stack-options", response_model=List[StackOption])
def get_ai_config_stack_options(
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    """Every stack this tenant can point an AI-Configuration slot at — the
    "MOD Stack" provider's picker source. GE.AIC.1-3."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    from services.ai_stack_provider import list_stack_options
    return list_stack_options(db, current_user.tenant_id)


# =============================================================================
# ai-config-stack-binding §6.1 — the binding surface (design D1/D5/D7)
# =============================================================================
# `ai_slot_binding` is the queryable truth (models.py:11097); the resolver
# (services/ai_slot_resolver.py) is the typed read. What lives HERE is the
# write path and the two Dashboard reads. Conventions honoured:
#   * `key_ref` is a Vault PATH and is never returned — only `key_ref_has_key`
#     is, the same bool-for-secret convention `api_key` already uses (:1626/:1993)
#     and the same one the alias migration writes with (services/ai_binding_migrate.py).
#   * No hard deletes: `enabled=false` + `retired_at` (ISO 27001 A.18.1.3).
#   * Writes are audited (append-only `AuditEvent`), like the neighbouring PUTs.
#   * No raw `db.query(` (lint R5): the reads go through `db.execute(select(...))`,
#     the idiom routers/nodes_builds.py and routers/platform_elevation.py already use.
from sqlalchemy import select
from sqlalchemy import func as _sa_func

from services.ai_slot_resolver import EMBEDDING_SLOTS, SLOTS, resolve_slot
from services.ai_stack_provider import (StackProviderError, stack_kind,
                                        _served_models_for)
from crud.persistent_stacks_domain import list_all_stacks_ordered
from schemas import (AiBindingResult, AiBindingTarget, AiBindingUpdate,
                     AiBindingsListResponse, AiSlotNameType, AiSlotResolution,
                     AiStackRow, AiStacksListResponse, StackHealth)


def _binding_live_row(db: Session, slot: str, scope: str,
                      tenant_id: Optional[int], priority: int):
    """The ENABLED row at one chain position, or None (lint-R5-clean)."""
    q = select(models.AiSlotBinding).where(
        models.AiSlotBinding.slot == slot,
        models.AiSlotBinding.scope == scope,
        models.AiSlotBinding.priority == priority,
        models.AiSlotBinding.enabled.is_(True))
    q = (q.where(models.AiSlotBinding.tenant_id == tenant_id) if scope == "tenant"
         else q.where(models.AiSlotBinding.tenant_id.is_(None)))
    return db.execute(q).scalars().first()


def _binding_slot_rows(db: Session, slot: str,
                       scope: Optional[str] = None) -> List[Any]:
    """Every row for a slot (live AND retired) — the history read."""
    q = select(models.AiSlotBinding).where(models.AiSlotBinding.slot == slot)
    if scope:
        q = q.where(models.AiSlotBinding.scope == scope)
    return db.execute(q).scalars().all()


def _binding_all_rows(db: Session) -> List[Any]:
    """Every binding row platform-wide, ordered (the ai-stacks 'used by' read)."""
    q = select(models.AiSlotBinding).order_by(models.AiSlotBinding.slot,
                                              models.AiSlotBinding.priority,
                                              models.AiSlotBinding.id)
    return db.execute(q).scalars().all()


def _slot_capability(slot: str) -> str:
    """'embedding' for the three *_embed slots, else 'chat' (models.py's CHECK)."""
    return "embedding" if slot in EMBEDDING_SLOTS else "chat"


def _binding_dto(res: Dict[str, Any]) -> AiSlotResolution:
    """The resolver's dict -> the DTO, with `key_ref` masked to a bool.

    The slot/capability identity travels in the resolver's `target` block
    (services/ai_slot_resolver._finish), so a resolution that has no target —
    nobody ever bound the slot — has no identity to carry either: callers
    must only call this when `res["target"]` is set.
    """
    target = dict(res.get("target") or {})
    key_ref = target.pop("key_ref", None)
    return AiSlotResolution(
        slot=target["slot"], capability=target["capability"],
        outcome=res["outcome"], message=res.get("message") or "",
        attempts=res.get("attempts") or [], progress=res.get("progress"),
        target=(AiBindingTarget(**{**target, "key_ref_has_key": bool(key_ref)}
                ) if target else None))


@router.get("/ai-config/bindings", response_model=AiBindingsListResponse)
def get_ai_config_bindings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Every AI slot, what serves it, and WHERE that answer came from.

    ai-config-stack-binding §6.1 (design D1/D5). One pass over the nine slots
    through :func:`services.ai_slot_resolver.resolve_slot`, so the page shows
    the SAME typed sentence the resolver gives a feature — "stack X is stopped",
    "nothing answers at host:port", "nobody picked" — never a blank.

    `rows` carries the slots that have a binding; `unbound_slots` the ones
    nobody ever configured, which is a different sentence on the page.
    Platform-admin gated, matching `/platform/managed-ai/setting` (:2655).
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    rows, unbound = [], []
    for slot in SLOTS:
        res = resolve_slot(db, slot)
        if res.get("target"):
            rows.append(_binding_dto(res))
        else:
            unbound.append(slot)
    return AiBindingsListResponse(rows=rows, unbound_slots=unbound)


@router.get("/ai-config/tenant-bindings", response_model=AiBindingsListResponse)
def get_ai_config_tenant_bindings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """This tenant's view of the same nine slots (Q-AI2 = A precedence).

    Tenant-gated exactly like `GET /ai-config/{tool}` (:3054): an active,
    onboarded user of the tenant — no platform-admin requirement, because a
    tenant's own override is precisely what a tenant user is allowed to see
    and set. A tenant that has rows for a slot sees ITS answer, not the
    platform's; a tenant that retired them all sees `no_binding` with
    "this tenant turned this slot off".
    """
    rows, unbound = [], []
    for slot in SLOTS:
        res = resolve_slot(db, slot, tenant_id=current_user.tenant_id)
        if res.get("target"):
            rows.append(_binding_dto(res))
        else:
            unbound.append(slot)
    return AiBindingsListResponse(rows=rows, unbound_slots=unbound)


@router.get("/ai-config/ai-stacks", response_model=AiStacksListResponse)
def get_ai_config_ai_stacks(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Every LLM stack: model, running/sleeping/down, live health, slots bound.

    The "AI stacks" half of the AI page (design D8, §7.1). Policy for it lives
    here because it stitches three existing readings into one:
      * the stack rows themselves (`list_all_stacks_ordered`),
      * the compose-level live verdict from
        :func:`services.ai_config_service.build_managed_llm_health` (:571) —
        asked ONCE per request, never once per stack,
      * the binding rows, so each stack shows the slots that use it ("used by",
        design D1's "which slots point at stack 42?" SQL read).
    A stack that serves no model and is bound by nothing is not an LLM stack,
    so it is left out rather than shown as an empty row.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    from services.ai_config_service import build_managed_llm_health
    # CE guard (GT.D4b pattern, precedent routers/embedding_models.py:118):
    # services.stack_observation is paid and absent on CE — the per-stack
    # compose health grade comes back absent (health=None) there.
    from utils.paid_import import optional_paid
    _so = optional_paid("services.stack_observation")
    from schemas import StackHealth

    health = build_managed_llm_health(db)
    served_live = [m for m in (health.served_models or []) if m]
    rows_by_stack: Dict[int, List[AiSlotResolution]] = {}
    for slot in SLOTS:
        res = resolve_slot(db, slot)
        tgt = res.get("target") or {}
        sid = tgt.get("stack_id")
        if sid:
            # Bound, whatever it is doing right now — a stopped stack still
            # SHOWS the slot that uses it (that is the point of the page: the
            # stack row says "assistant uses me", and its own status line says
            # whether it is serving).
            rows_by_stack.setdefault(int(sid), []).append(_binding_dto(res))

    all_rows = _binding_all_rows(db)
    out: List[AiStackRow] = []
    for st in list_all_stacks_ordered(db):
        mine = [r for r in all_rows if r.stack_id == st.id]
        live = [r for r in mine if r.enabled]
        kind = stack_kind(st)
        served = _served_models_for(st)
        if not mine and not served:
            continue          # not an LLM stack — nothing to say about it
        used = rows_by_stack.get(st.id, [])
        healthy: Optional[bool] = None
        live_error = None
        if health.error:
            healthy, live_error = False, str(health.error)
        elif served:
            healthy = any(m in served_live for m in served)
        else:
            healthy = bool(health.healthy)
        out.append(AiStackRow(
            stack_id=st.id, name=st.name, status=st.status, kind=kind,
            model=(served[0] if served else None), served_models=served,
            health=(StackHealth(**_so.stack_health_grade(db, st))
                    if _so is not None else None),
            live_healthy=healthy,
            live_served_models=[m for m in served_live if m in served],
            live_error=live_error,
            used_by=sorted(used, key=lambda r: (r.target.priority or 0
                                                if r.target else 0)),
            binding_count=len(live), retired_binding_count=len(mine) - len(live),
        ))
    return AiStacksListResponse(
        stacks=out,
        managed_health=health.model_dump() if health is not None else None,
    )


@router.put("/ai-config/bindings/{slot}", response_model=AiBindingResult)
def put_ai_config_bindings(
    slot: AiSlotNameType,
    body: AiBindingUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Set the platform's default binding for one AI slot.

    Platform-admin gated, matching `/platform/managed-ai/setting`'s PUT (:2655)
    — these rows are the platform defaults every tenant inherits.

    An unknown slot is a 422 from the path parameter itself (`AiSlotNameType`
    is the same nine names models.py's `ck_ai_slot_binding_slot` allows), so
    a typo can never create a tenth slot the resolver would ignore.

    Partial like every other update endpoint here: a live row at
    `(slot, 'platform', priority)` is patched, otherwise a NEW row is written
    (which must name a `target_kind`). `enabled=false` retires the row —
    `retired_at` is stamped and the row is NEVER deleted (ISO 27001), which is
    also why reviving a position writes a new row rather than clearing a flag.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    return _write_binding(db, slot, "platform", None, body, current_user)


@router.put("/ai-config/tenant-bindings/{slot}", response_model=AiBindingResult)
def put_ai_config_tenant_bindings(
    slot: AiSlotNameType,
    body: AiBindingUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """This tenant's own binding for one slot — the override (Q-AI2 = A).

    Tenant-gated exactly like `PUT /ai-config/{tool}` (:3065): an active,
    onboarded user of the tenant. No platform-admin requirement, because this
    write is scoped to `current_user.tenant_id` and is precisely the thing a
    tenant is allowed to do: its row shadows the platform row for that slot,
    and retiring it means that tenant gets `no_binding` for that slot rather
    than silently inheriting the platform chain (5.1).
    """
    return _write_binding(db, slot, "tenant", current_user.tenant_id, body,
                          current_user)


def _write_binding(db: Session, slot: str, scope: str,
                   tenant_id: Optional[int], body: AiBindingUpdate,
                   current_user: models.User) -> AiBindingResult:
    """The one write path both scopes share (validation + audit)."""
    from services.ai_stack_provider import validate_stack_choice
    from utils.egress_policy import (CATEGORY_EXTERNAL_LLM, refuse_reason,
                                     url_is_external)

    priority = int(body.priority)
    if priority < 0 or priority > 15:
        raise HTTPException(
            status_code=422,
            detail=f"priority must be 0 (primary) or 1..15 (the ordered "
                   f"fallbacks); {priority} is outside that chain")

    capability = _slot_capability(slot)
    row = _binding_live_row(db, slot, scope, tenant_id, priority)

    target_kind = body.target_kind or (row.target_kind if row else None)
    if not target_kind:
        raise HTTPException(
            status_code=422,
            detail=f"No binding exists for the {slot} slot at position {priority}; "
                   "a new one must name a target_kind (stack | external | auto)")

    stack_id = body.stack_id if body.stack_id is not None else (
        row.stack_id if row else None)
    stack_name = row.stack_name if row else None
    base_url = body.base_url if body.base_url is not None else (
        row.base_url if row else None)
    model = body.model if body.model is not None else (row.model if row else None)
    key_ref = body.key_ref if body.key_ref is not None else (
        row.key_ref if row else None)

    if target_kind == "stack":
        if not stack_id:
            raise HTTPException(
                status_code=422,
                detail="a 'stack' binding must name the stack_id that serves it")
        try:
            _st, _served = validate_stack_choice(
                db, int(stack_id), body.served_model or model,
                # Q-AI6 = A: a tenant may bind a PLATFORM stack, so the save-time
                # scope check is skipped here (the resolver looks stacks up
                # unscoped too) — the kind check still runs.
                None, kind=capability)
            stack_name = _st.name
        except StackProviderError as sp_err:
            # Curated, user-facing text from the shared save-time validator
            # ("Stack '#5' serves embedding models, not chat — choose a chat
            # stack"), never an internal trace.
            raise HTTPException(status_code=422, detail=str(sp_err)) from None
        stack_id = int(stack_id)
        # The validator resolved which served model answers (a stack may serve
        # several and the binding may have named none) — store THAT, so the
        # resolver pins the same model the operator saw.
        model = _served
        base_url = key_ref = None
    elif target_kind == "external":
        if not (base_url or "").strip():
            raise HTTPException(
                status_code=422,
                detail="an 'external' binding must name the base_url it dials")
        stack_id = stack_name = None
        # GSEC R-042: an external endpoint is refused while the platform's
        # egress switch is off — the binding must not be creatable and then
        # silently unusable.
        if url_is_external(base_url):
            _egress_refusal = refuse_reason(CATEGORY_EXTERNAL_LLM, db=db)
            if _egress_refusal:
                raise HTTPException(
                    status_code=422,
                    detail=(f"External LLM egress is disabled on this platform "
                            f"({_egress_refusal}). Bind this slot to a MOD-hosted "
                            f"(local/stack) endpoint, or re-enable the setting."))
    else:  # 'auto' — Let MOD choose: nothing to name
        stack_id = stack_name = base_url = model = key_ref = None

    enabled = bool(body.enabled)
    if row:
        row.slot, row.scope = slot, scope
        row.tenant_id = tenant_id
        row.target_kind = target_kind
        row.stack_id, row.stack_name = stack_id, stack_name
        row.base_url, row.model, row.key_ref = base_url, model, key_ref
        row.capability, row.priority, row.enabled = capability, priority, enabled
        if not enabled:
            row.retired_at = _sa_func.now()
        saved = row
    else:
        saved = models.AiSlotBinding(
            slot=slot, scope=scope, tenant_id=tenant_id,
            target_kind=target_kind, stack_id=stack_id, stack_name=stack_name,
            base_url=base_url, model=model, key_ref=key_ref,
            capability=capability, priority=priority, enabled=enabled,
            created_by_user_id=current_user.id)
        if not enabled:
            saved.retired_at = _sa_func.now()
        db.add(saved)

    db.add(models.AuditEvent(
        tenant_id=tenant_id, user_id=current_user.id,
        event_name=("ai_binding_retired" if not enabled else "ai_binding_set"),
        details={"slot": slot, "scope": scope, "priority": priority,
                 "target_kind": target_kind, "stack_id": stack_id,
                 "stack_name": stack_name, "base_url": base_url,
                 "model": model, "capability": capability,
                 "key_ref": key_ref,
                 "key_ref_has_key": bool(key_ref),
                 "enabled": enabled,
                 "by": f"user:{current_user.id}"}))
    db.commit()

    res = resolve_slot(db, slot, tenant_id=tenant_id)
    return AiBindingResult(
        slot=slot, scope=scope, tenant_id=tenant_id, priority=priority,
        target_kind=target_kind, stack_id=stack_id, stack_name=stack_name,
        base_url=base_url, model=model, capability=capability, enabled=enabled,
        key_ref_has_key=bool(key_ref),
        resolution=(_binding_dto(res) if res.get("target") else None),
        message=(f"Slot '{slot}' position {priority} now binds {target_kind}"
                 + (f" (stack '{stack_name}' #{stack_id})" if stack_id else "")
                 + (f" at {base_url}" if target_kind == "external" else "")
                 + (" — retired" if not enabled else "")))


# =============================================================================
# owner BUG.B59b — assistant knowledge-SOURCE bindings (the picker's rows)
# =============================================================================
# `ai_knowledge_source_binding` (models.py:11251) is BUG.B59's split of the
# assistant_knowledge_embed slot: MOD Docs, a tenant's own files and tenant
# uploads are retrieved with DIFFERENT embedding models, so the picker needs
# one row per (scope, tenant, source_kind) instead of one chain per slot.
# Everything the slot surface above already guarantees is honoured here
# verbatim: `key_ref` is a Vault PATH and never leaves the wire (only
# `key_ref_has_key` does), a retired row is soft (enabled=false + retired_at,
# ISO 27001 A.18.1.3 — never a hard delete), writes are audited, the reads go
# through `db.execute(select(...))` (lint R5), and a stack target must be
# EMBEDDING-capable — the same capability check the slot surface runs for
# EMBEDDING_SLOTS (services/ai_stack_provider.validate_stack_choice(kind=...)).
from schemas import (AiKnowledgeSourceBindingResult,
                     AiKnowledgeSourceBindingRow,
                     AiKnowledgeSourceBindingUpdate,
                     AiKnowledgeSourceKindType,
                     AiKnowledgeSourceRow,
                     AiKnowledgeSourcesListResponse)

KNOWLEDGE_SOURCE_KINDS = ("mod_docs", "tenant_files", "uploads")
"""The three sources the assistant searches (models.py ck_akb_source_kind).

A path parameter typed as `AiKnowledgeSourceKindType`, so a typo is a 422 from
the path itself and can never create a fourth kind the picker would ignore —
exactly how `AiSlotNameType` guards the slot PUTs."""


def _kb_live_row(db: Session, source_kind: str, scope: str,
                 tenant_id: Optional[int]):
    """The ENABLED row for one source at one scope, or None (lint-R5-clean)."""
    q = select(models.AiKnowledgeSourceBinding).where(
        models.AiKnowledgeSourceBinding.source_kind == source_kind,
        models.AiKnowledgeSourceBinding.scope == scope,
        models.AiKnowledgeSourceBinding.enabled.is_(True))
    q = (q.where(models.AiKnowledgeSourceBinding.tenant_id == tenant_id)
         if scope == "tenant" else
         q.where(models.AiKnowledgeSourceBinding.tenant_id.is_(None)))
    return db.execute(q).scalars().first()


def _kb_tenant_spoken_row(db: Session, source_kind: str,
                          tenant_id: int):
    """The tenant's most recent row for a kind, live OR retired (the 5.1 read).

    A tenant that retired its row has TURNED that source off — the page must
    say so rather than silently showing the platform row, so the tombstone is
    what the GET surfaces."""
    q = select(models.AiKnowledgeSourceBinding).where(
        models.AiKnowledgeSourceBinding.source_kind == source_kind,
        models.AiKnowledgeSourceBinding.scope == "tenant",
        models.AiKnowledgeSourceBinding.tenant_id == tenant_id)
    return db.execute(q.order_by(
        models.AiKnowledgeSourceBinding.updated_at.desc(),
        models.AiKnowledgeSourceBinding.id.desc())).scalars().first()


def _kb_dto(row) -> AiKnowledgeSourceBindingRow:
    """A row -> the DTO, with `key_ref` masked to a bool (never the PATH)."""
    return AiKnowledgeSourceBindingRow(
        scope=row.scope, tenant_id=row.tenant_id, source_kind=row.source_kind,
        target_kind=row.target_kind, stack_id=row.stack_id,
        stack_name=row.stack_name, served_model=row.served_model,
        base_url=row.base_url, model=row.model,
        capability="embedding", enabled=bool(row.enabled),
        key_ref_has_key=bool(row.key_ref), retired_at=row.retired_at,
        created_at=row.created_at, updated_at=row.updated_at)


@router.get("/ai-config/knowledge-sources",
            response_model=AiKnowledgeSourcesListResponse)
def get_ai_config_knowledge_sources(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Every knowledge source, what embeds it, and WHERE that answer came from.

    BUG.B59b's read of the picker: one row per source kind (MOD Docs, this
    tenant's files, this tenant's uploads), each carrying the platform's
    binding (or null when nobody ever bound it) plus THIS tenant's override
    when the caller has a tenant. `effective_scope` names which row actually
    answers — a tenant row shadows the platform row, exactly like the slot
    surface's Q-AI2 = A precedence — and `unbound` is the "nobody ever picked"
    sentence, kept distinct from a source whose binding exists and is merely
    retired.

    Gated like `GET /platform/ai-config/tenant-bindings` (:3217): an active,
    onboarded user, no platform-admin requirement — a tenant's own override is
    precisely what a tenant user is allowed to see, and the platform rows are
    the defaults that tenant inherits.
    """
    rows, unbound = [], []
    for kind in KNOWLEDGE_SOURCE_KINDS:
        plat = _kb_live_row(db, kind, "platform", None)
        tenant_live = tenant_any = None
        if current_user.tenant_id is not None:
            tenant_live = _kb_live_row(db, kind, "tenant",
                                       current_user.tenant_id)
            # A retired override still "spoke" — show the tombstone so the page
            # can say "this tenant turned this source off" (the slot surface's
            # 5.1 rule: a tenant that spoke does NOT inherit the platform).
            tenant_any = tenant_live or _kb_tenant_spoken_row(
                db, kind, current_user.tenant_id)
        # Q-AI2 = A precedence: the tenant's row answers when the tenant has one;
        # a tenant that retired it has TURNED the source off, so nothing answers
        # for it — that is a different sentence from "nobody ever picked". A
        # platform-admin caller reads the PLATFORM view (exactly how
        # `GET /platform/ai-config/bindings` resolves with tenant_id=None), so
        # its own tenant's override is shown in `tenant_binding` but does not
        # shadow the platform answer it is inspecting.
        effective = (plat if current_user.is_platform_admin else
                     (tenant_live if tenant_any else plat))
        if effective is None:
            unbound.append(kind)
        rows.append(AiKnowledgeSourceRow(
            source_kind=kind,
            platform_binding=(_kb_dto(plat) if plat else None),
            tenant_binding=(_kb_dto(tenant_any) if tenant_any else None),
            effective_scope=(effective.scope if effective else None),
            effective_binding=(_kb_dto(effective) if effective else None),
            unbound=effective is None))
    return AiKnowledgeSourcesListResponse(rows=rows,
                                         unbound_source_kinds=unbound)


@router.put("/ai-config/knowledge-sources/{source_kind}",
            response_model=AiKnowledgeSourceBindingResult)
def put_ai_config_knowledge_sources(
    source_kind: AiKnowledgeSourceKindType,
    body: AiKnowledgeSourceBindingUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Set the platform's embedding model for one knowledge source.

    Platform-admin gated, matching `/platform/ai-config/bindings/{slot}`
    (:3314) — these rows are the platform defaults every tenant inherits.

    An unknown source kind is a 422 from the path parameter itself
    (`AiKnowledgeSourceKindType` is the same three names models.py's
    `ck_akb_source_kind` allows), so a typo can never create a fourth kind the
    picker would ignore.

    Partial like every other update endpoint here: the live row at
    `(source_kind, 'platform')` is patched, otherwise a NEW row is written
    (which must name a `target_kind`). `enabled=false` retires the row —
    `retired_at` is stamped and the row is NEVER deleted (ISO 27001), which is
    also why reviving a retired source writes a new row rather than clearing a
    flag.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    return _write_knowledge_binding(db, source_kind, "platform", None, body,
                                    current_user)


@router.put("/ai-config/knowledge-sources-tenant/{source_kind}",
            response_model=AiKnowledgeSourceBindingResult)
def put_ai_config_knowledge_sources_tenant(
    source_kind: AiKnowledgeSourceKindType,
    body: AiKnowledgeSourceBindingUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """This tenant's own embedding model for one source — the override.

    Tenant-gated exactly like `PUT /platform/ai-config/tenant-bindings/{slot}`
    (:3341): an active, onboarded user of the tenant, no platform-admin
    requirement, because the write is scoped to `current_user.tenant_id` and
    is precisely what a tenant is allowed to do — its row shadows the
    platform row for that source, and retiring it means that source is turned
    OFF for this tenant rather than silently inheriting the platform model.
    """
    if current_user.tenant_id is None:
        raise HTTPException(
            status_code=422,
            detail="this user has no tenant to write an override for")
    return _write_knowledge_binding(db, source_kind, "tenant",
                                    current_user.tenant_id, body,
                                    current_user)


def _write_knowledge_binding(db: Session, source_kind: str, scope: str,
                             tenant_id: Optional[int],
                             body: AiKnowledgeSourceBindingUpdate,
                             current_user: models.User
                             ) -> AiKnowledgeSourceBindingResult:
    """The one write path both scopes share (validation + audit)."""
    from services.ai_stack_provider import validate_stack_choice
    from utils.egress_policy import (CATEGORY_EXTERNAL_LLM, refuse_reason,
                                     url_is_external)

    row = _kb_live_row(db, source_kind, scope, tenant_id)

    target_kind = body.target_kind or (row.target_kind if row else None)
    if not target_kind:
        raise HTTPException(
            status_code=422,
            detail=f"No binding exists for the {source_kind} knowledge source; "
                   "a new one must name a target_kind (stack | external | auto)")

    stack_id = body.stack_id if body.stack_id is not None else (
        row.stack_id if row else None)
    stack_name = row.stack_name if row else None
    served_model = (body.served_model if body.served_model is not None else
                    (row.served_model if row else None))
    base_url = body.base_url if body.base_url is not None else (
        row.base_url if row else None)
    model = body.model if body.model is not None else (row.model if row else None)
    key_ref = body.key_ref if body.key_ref is not None else (
        row.key_ref if row else None)

    # A knowledge source is EMBEDDED, never chatted — so the stack target must
    # be embedding-capable. This is the SAME capability check the slot surface
    # runs for EMBEDDING_SLOTS (validate_stack_choice(kind=capability)), which
    # also resolves which served model answers and refuses a stack that does
    # not exist.
    if target_kind == "stack":
        if not stack_id:
            raise HTTPException(
                status_code=422,
                detail="a 'stack' knowledge-source binding must name the "
                       "stack_id that embeds it")
        try:
            _st, _served = validate_stack_choice(
                db, int(stack_id), served_model or model,
                # Q-AI6 = A (inherited): a tenant may bind a PLATFORM stack, so
                # the scope check is skipped — the kind check still runs.
                None, kind="embedding")
            stack_name = _st.name
        except StackProviderError as sp_err:
            # Curated, user-facing text from the shared save-time validator
            # ("Stack '#5' serves chat models, not embedding — choose an
            # embedding stack"), never an internal trace.
            raise HTTPException(status_code=422, detail=str(sp_err)) from None
        stack_id = int(stack_id)
        # The validator resolved which served model answers — store THAT so the
        # picker pins the same model the operator saw.
        served_model = _served
        model = _served
        base_url = key_ref = None
    elif target_kind == "external":
        if not (base_url or "").strip():
            raise HTTPException(
                status_code=422,
                detail="an 'external' knowledge-source binding must name the "
                       "base_url it dials")
        stack_id = stack_name = None
        # GSEC R-042 (inherited): an external endpoint is refused while the
        # platform's egress switch is off — the binding must not be creatable
        # and then silently unusable.
        if url_is_external(base_url):
            _egress_refusal = refuse_reason(CATEGORY_EXTERNAL_LLM, db=db)
            if _egress_refusal:
                raise HTTPException(
                    status_code=422,
                    detail=(f"External LLM egress is disabled on this platform "
                            f"({_egress_refusal}). Bind this source to a MOD-hosted "
                            f"(local/stack) endpoint, or re-enable the setting."))
    else:  # 'auto' — Let MOD choose: nothing to name
        stack_id = stack_name = served_model = base_url = model = key_ref = None

    enabled = bool(body.enabled)
    if row:
        row.scope, row.tenant_id = scope, tenant_id
        row.source_kind = source_kind
        row.target_kind = target_kind
        row.stack_id, row.stack_name = stack_id, stack_name
        row.served_model = served_model
        row.base_url, row.model, row.key_ref = base_url, model, key_ref
        row.enabled = enabled
        if not enabled:
            row.retired_at = _sa_func.now()
        saved = row
    else:
        saved = models.AiKnowledgeSourceBinding(
            source_kind=source_kind, scope=scope, tenant_id=tenant_id,
            target_kind=target_kind, stack_id=stack_id, stack_name=stack_name,
            served_model=served_model, base_url=base_url, model=model,
            key_ref=key_ref, enabled=enabled,
            created_by_user_id=current_user.id)
        if not enabled:
            saved.retired_at = _sa_func.now()
        db.add(saved)

    db.add(models.AuditEvent(
        tenant_id=tenant_id, user_id=current_user.id,
        event_name=("ai_knowledge_source_binding_retired" if not enabled
                    else "ai_knowledge_source_binding_set"),
        details={"source_kind": source_kind, "scope": scope,
                 "target_kind": target_kind, "stack_id": stack_id,
                 "stack_name": stack_name, "base_url": base_url,
                 "model": model, "served_model": served_model,
                 "capability": "embedding",
                 "key_ref": key_ref, "key_ref_has_key": bool(key_ref),
                 "enabled": enabled,
                 "by": f"user:{current_user.id}"}))
    db.commit()

    return AiKnowledgeSourceBindingResult(
        source_kind=source_kind, scope=scope, tenant_id=tenant_id,
        target_kind=target_kind, stack_id=stack_id, stack_name=stack_name,
        served_model=served_model, base_url=base_url, model=model,
        capability="embedding", enabled=enabled,
        key_ref_has_key=bool(key_ref),
        retired_at=(saved.retired_at if not enabled else None),
        message=(f"Knowledge source '{source_kind}' now binds {target_kind}"
                 + (f" (stack '{stack_name}' #{stack_id} / {served_model})"
                    if stack_id else "")
                 + (f" at {base_url}" if target_kind == "external" else "")
                 + (" — retired" if not enabled else "")))


# =============================================================================
# FIX-CE-ASSISTANT-MODEL-PICKER-P1 — the Settings -> AI assistant-model picker
# =============================================================================
# `assistant.model_choice` is registered in routers/support_chat/_shared.py
# (:4611, tenant row > platform row) and the reader already consults it BEFORE
# the slot chain (:613) — what was missing was the WRITE. The only generic
# writer is platform-admin gated (platform_settings.py:412), so a CE tenant
# user could never pick, and every assistant turn died on the reasoner alias
# that has no container on CE (owner Q-B31). These two paths are the tenant-
# scoped surface the Dashboard picker (CORE/Dashboard/js/assistant-model-
# picker.js, AMC_ENDPOINT) calls:
#   GET -> {configured, options:[{token,label,kind,model,status}]}
#   PUT {token} -> the same shape ("" clears the pick)
# Tenant isolation: the tenant ALWAYS comes from the authenticated principal,
# never the body; options are THIS tenant's LLM stacks only; a `stack:<id>`
# naming another tenant's stack is refused (422) and the other tenant's name
# never travels. There is no per-tenant external-LLM-connection model, so
# options enumerate STACKS only — an `external:<url>` token is accepted by
# shape (the same http(s):// test the reader applies in `_dial_choice`) and
# its url is never enumerated and never logged. Fail closed: a malformed or
# foreign token is the reader's OWN 422 sentence (`_ASSISTANT_BAD_TOKEN`), so
# the Settings hint and the turn hint are the same sentence. The key is NEVER
# re-registered here — the platform-settings registry at _shared.py:4611 owns
# it, and the write goes through `set_setting` (validation + append-only audit,
# the caller owns the commit).

from routers.assistant import _is_tenant_admin
from routers.support_chat._shared import _ASSISTANT_BAD_TOKEN
from services import platform_settings as _amc_psr
from crud.persistent_stacks_domain import get_stack_by_id as _amc_stack_row

ASSISTANT_MODEL_CHOICE_KEY = "assistant.model_choice"


class AssistantModelOption(BaseModel):
    """One model this tenant may pick for the assistant / support surface."""
    token: str
    label: str
    kind: str
    model: Optional[str] = None
    status: Optional[str] = None


class AssistantModelChoiceDTO(BaseModel):
    """The picker DTO: what is configured now + what this tenant may pick."""
    configured: str
    options: List[AssistantModelOption]


class AssistantModelChoiceUpdate(BaseModel):
    """The PUT body: the picker token ONLY (no tenant, no model, no url)."""
    token: str


def _assistant_choice_usable(db: Session, tenant_id: Optional[int],
                             token: str) -> bool:
    """True when `token` names something THIS tenant can actually use.

    The reader's own shape test (`_dial_choice`, _shared.py:516): `stack:<id>`
    or `external:<url>`, optional `|<model>`. `external:` is validated by
    SHAPE only (there is no per-tenant external-connection model to look up)
    and the url is never logged. `stack:` must name a stack this tenant owns
    — another tenant's row is refused here, by id, and its name never
    travels."""
    token = (token or "").strip()
    kind, sep, rest = token.partition(":")
    kind, rest = kind.strip().lower(), rest.strip()
    if "|" in rest:
        rest = rest.split("|", 1)[0].strip()
    if not sep or kind not in ("stack", "external") or not rest:
        return False
    if kind == "external":
        return rest.lower().startswith(("http://", "https://"))
    try:
        stack_id = int(rest)
    except ValueError:
        return False
    row = _amc_stack_row(db, stack_id)
    return row is not None and getattr(row, "tenant_id", None) == tenant_id


@router.get("/ai-config/assistant-model-choice",
            response_model=AssistantModelChoiceDTO)
def get_ai_config_assistant_model_choice(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """This tenant's assistant-model pick and the models it may pick instead.

    Gated exactly like `GET /ai-config/tenant-bindings` (:3162): an active,
    onboarded user of the tenant — a tenant MEMBER may READ (the picker needs
    the options to render at all), no platform-admin requirement, because
    what is read is precisely this tenant's own answer. The tenant comes from
    the principal, never the request.

    Options are THIS tenant's LLM stacks only — `list_all_stacks_ordered`
    filtered to the caller's tenant and to stacks that serve a model, the
    same projection the AI-stacks page applies (:3226). Another tenant's
    stack is never listed, so a member can never even be offered it.
    """
    configured = str(_amc_psr.get_setting(
        ASSISTANT_MODEL_CHOICE_KEY, db=db,
        tenant_id=current_user.tenant_id) or "")
    options: List[AssistantModelOption] = []
    for st in list_all_stacks_ordered(db):
        if st.tenant_id != current_user.tenant_id:
            continue
        served = _served_models_for(st)
        if not served:
            continue          # not an LLM stack — nothing to pick
        options.append(AssistantModelOption(
            token=f"stack:{st.id}", label=st.name, kind=stack_kind(st),
            model=served[0], status=st.status))
    return AssistantModelChoiceDTO(configured=configured, options=options)


@router.put("/ai-config/assistant-model-choice",
            response_model=AssistantModelChoiceDTO)
def put_ai_config_assistant_model_choice(
    body: AssistantModelChoiceUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Write this tenant's assistant-model pick (the Settings -> AI picker).

    Tenant-root-admin only — the same bail `assistant_knowledge.py:78` uses
    (`_is_tenant_admin`: platform admin or the tenant's root admin), 403
    "Tenant admin only"; a tenant MEMBER gets that 403 and the picker shows
    the server's detail verbatim.

    `""` clears the pick (the tenant row goes back to the registered default,
    so the reader answers "nothing configured" instead of dialling a dead
    alias). Any other token must name something this tenant can use: a
    malformed or foreign token is the reader's OWN 422 sentence, so the
    Settings hint and the turn hint match and nothing is written. The write
    is the tenant row (`tenant_id` from the principal) through
    `set_setting` — the registry validates it, audits an append-only row, and
    this caller owns the commit.
    """
    if not _is_tenant_admin(current_user):
        raise HTTPException(status_code=403, detail="Tenant admin only")

    token = (body.token or "").strip()
    if token and not _assistant_choice_usable(db, current_user.tenant_id,
                                             token):
        raise HTTPException(status_code=422, detail=_ASSISTANT_BAD_TOKEN)

    _amc_psr.set_setting(
        ASSISTANT_MODEL_CHOICE_KEY, token, db=db,
        actor_user_id=current_user.id, tenant_id=current_user.tenant_id)
    db.commit()
    return get_ai_config_assistant_model_choice(
        db=db, current_user=current_user)


@router.get("/ai-config/{tool}", response_model=ToolAIConfig)
def get_tool_ai_config(
    tool: ToolName,
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Get one tool's AI config. api_key is returned as a bool (never raw)."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    return _resolve_config(tool, _read_tool_config(tool))


@router.put("/ai-config/{tool}", response_model=ToolAIConfig)
def update_tool_ai_config(
    tool: ToolName,
    update: ToolAIConfigUpdate,
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    """Update one tool's AI config. Only fields the caller sends are touched."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")

    config = _read_tool_config(tool)

    if update.execution_mode is not None:
        config["execution_mode"] = update.execution_mode
    if update.provider is not None:
        config["provider"] = update.provider
    if update.model is not None:
        config["model"] = update.model
    if update.stack_id is not None:
        config["stack_id"] = update.stack_id
    if update.served_model is not None:
        config["served_model"] = update.served_model
    # GE.AIC.1-3 — provider == "stack" is validated against the running
    # PersistentStack fleet at SAVE time (exists, in this tenant's scope,
    # serves the named model). Not required to be `running` right now — an
    # operator may pin a stopped stack ahead of starting it; call-time
    # resolution (`_resolved_effective_env`) is what enforces `running`.
    if config.get("provider") == "stack":
        from services.ai_stack_provider import validate_stack_choice, StackProviderError
        try:
            _stack, resolved_served_model = validate_stack_choice(
                db, config.get("stack_id"), config.get("served_model"),
                current_user.tenant_id, kind="chat")
        except StackProviderError as e:
            raise HTTPException(status_code=422, detail=str(e))
        config["served_model"] = resolved_served_model
    if update.api_key is not None:
        # Empty string clears the key; any other value replaces it.
        if update.api_key == "":
            config.pop("api_key", None)
        else:
            config["api_key"] = update.api_key
    if update.base_url is not None:
        config["base_url"] = update.base_url
    if update.compute_target is not None:
        config["compute_target"] = update.compute_target
    if update.keep_alive is not None:
        config["keep_alive"] = update.keep_alive
    if update.temperature is not None:
        config["temperature"] = update.temperature
    if update.max_tokens is not None:
        config["max_tokens"] = update.max_tokens
    if update.local_preset is not None:
        config["local_preset"] = update.local_preset
    if update.local_model is not None:
        # mesh-native-llm-routing §4.3 — a model that is not catalogued but
        # that a running stack serves is a legal pin; only a name nothing
        # serves is rejected.
        if update.local_model not in MODEL_CATALOG and \
                _stack_model_view(update.local_model, db=None) is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown local model {update.local_model!r}. "
                    f"Choose from the catalogued models "
                    f"{sorted(MODEL_CATALOG)} or a model served by a running "
                    f"stack."
                ),
            )
        config["local_model"] = update.local_model
    if update.critic_mode is not None:
        # WIZARD ONLY — the Node Wizard's code-review model choice
        # (self / reasoner / off). Ignored for other tools.
        if tool == "wizard":
            config["critic_mode"] = update.critic_mode
    # mesh-native-llm-routing §4.8 — opt-in per-tool LLM fallback. Empty
    # string clears the target (→ the tool's binding stands alone again).
    if update.fallback_enabled is not None:
        config["fallback_enabled"] = bool(update.fallback_enabled)
    if update.fallback_tool is not None:
        if update.fallback_tool == "":
            config.pop("fallback_tool", None)
        else:
            if update.fallback_tool not in CONFIGURABLE_TOOLS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unknown fallback tool {update.fallback_tool!r}. "
                        f"Choose from {list(CONFIGURABLE_TOOLS)}."
                    ),
                )
            if update.fallback_tool == tool:
                raise HTTPException(
                    status_code=400,
                    detail="A tool cannot fall back to itself.",
                )
            config["fallback_tool"] = update.fallback_tool
    # --- Embedding-model pins ------------------------------------------------
    # Node Wizard: single `embedding_model` → wizard pgvector store.
    # AI Assistant: `embedding_store` sub-choice + per-store pins. A pin change
    # queues a re-embed of ONLY that store. Empty string clears (→ default).
    #
    # GE.AIC.1-3 — each store also gets a provider switch ("platform" | "stack",
    # `embedding_provider[_user_rag|_docs]`) + matching `embedding_stack_id...`.
    # Assign those BEFORE _apply_pin runs so it sees this request's choice.
    if update.embedding_provider is not None:
        config["embedding_provider"] = update.embedding_provider
    if update.embedding_stack_id is not None:
        config["embedding_stack_id"] = update.embedding_stack_id
    if update.embedding_provider_user_rag is not None:
        config["embedding_provider_user_rag"] = update.embedding_provider_user_rag
    if update.embedding_stack_id_user_rag is not None:
        config["embedding_stack_id_user_rag"] = update.embedding_stack_id_user_rag
    if update.embedding_provider_docs is not None:
        config["embedding_provider_docs"] = update.embedding_provider_docs
    if update.embedding_stack_id_docs is not None:
        config["embedding_stack_id_docs"] = update.embedding_stack_id_docs

    _emb_targets: set = set()

    def _apply_pin(field: str, target: Optional[str],
                    provider_field: Optional[str] = None,
                    stack_id_field: Optional[str] = None) -> None:
        val = getattr(update, field)
        provider = config.get(provider_field) if provider_field else None
        if val is None and provider != "stack":
            return
        prev = config.get(field) or ""
        new = (val if val is not None else prev) or ""
        if provider == "stack":
            # The value IS the served-model name (optional — auto-picked when
            # the stack serves exactly one). Validated + resolved against the
            # stack's fleet rather than the MinIO-uploaded-models check below.
            from services.ai_stack_provider import validate_stack_choice, StackProviderError
            stack_id = config.get(stack_id_field) if stack_id_field else None
            try:
                _stack, new = validate_stack_choice(
                    db, stack_id, new or None, current_user.tenant_id,
                    kind="embedding")
            except StackProviderError as e:
                raise HTTPException(status_code=422, detail=str(e))
        elif new and new != prev:
            # Only allow pinning a model that's actually self-hosted in MinIO.
            # Otherwise the pin would point the store at a model that can't load
            # → failed migration / empty RAG. Reject up front (before the Vault
            # write) so the admin fixes it explicitly.
            from utils import embedding_model_store as _ems
            if not _ems.model_exists(new):
                raise HTTPException(status_code=400, detail=(
                    f"Embedding model '{new}' isn't available. Upload it under "
                    f"Settings → AI Configuration → Models first, then pin it."
                ))
        if new == "":
            config.pop(field, None)
        else:
            config[field] = new
        if target and new != prev:
            _emb_targets.add(target)

    if tool == "wizard":
        _apply_pin("embedding_model", "wizard", "embedding_provider", "embedding_stack_id")
    elif tool == "assistant":
        if update.embedding_store is not None:
            config["embedding_store"] = update.embedding_store
        _apply_pin("embedding_model_user_rag", "user_rag",
                   "embedding_provider_user_rag", "embedding_stack_id_user_rag")
        _apply_pin("embedding_model_docs", "docs",
                   "embedding_provider_docs", "embedding_stack_id_docs")
    else:
        _apply_pin("embedding_model", None)  # persist only; no store to migrate

    # Default when nothing has ever been set: Local (managed).
    config.setdefault("execution_mode", "local")

    _write_vault_path(_vault_path_for(tool), config)

    # Changing a store's embedding model means its vectors are the wrong width —
    # kick off that store's re-embed migration. The Dashboard approval gate
    # already warned the admin. Non-fatal: a failure here never blocks the save.
    # CE: docs target is denied — the route short-circuits with 402 before
    # _em.start() (the _run_docs() guard returns None, so _status stays "idle").
    for _target in _emb_targets:
        if _target == "docs":
            from utils.paid_import import optional_paid
            if optional_paid("utils.docs_ingester") is None:
                logger.info("CE: skipping docs re-embed (target=docs denied)")
                continue
        try:
            from utils import embedding_migration as _em
            started = _em.start(target=_target)
            logger.info("%s embedding pin changed → %s migration %s",
                        tool, _target,
                        "started" if started.get("started") else "already running")
        except Exception as e:  # noqa: BLE001
            logger.warning("could not start %s embedding migration (non-fatal): %s", _target, e)
    logger.info(
        "AI config updated (tool=%s) by user %s: mode=%s provider=%s model=%s preset=%s",
        tool, current_user.id,
        config.get("execution_mode"), config.get("provider"), config.get("model"),
        config.get("local_preset"),
    )

    # Reconcile managed-Ollama residency. Pass the just-saved config as the
    # authoritative view for THIS tool (Vault may not have replicated yet)
    # and re-read the other two from Vault.
    should_be_resident = _any_tool_in_local_mode(skip_tool=tool, override=config)
    target_preset = _active_preset_for_managed_ollama(skip_tool=tool, override=config) if should_be_resident else None
    _reconcile_managed_ollama_residency(should_be_resident, target_preset)

    # Drop the wizard's cached context-length so the next prompt build
    # picks up the new preset's num_ctx (or the new endpoint's
    # max_model_len if execution_mode changed). Without this, the
    # repair loop's char caps would keep tracking the OLD preset until
    # the API restarts.
    try:
        from node_builder.llm_budget import invalidate_cache as _invalidate_llm_budget_cache
        _invalidate_llm_budget_cache()
    except Exception as e:
        logger.warning("ai_config save: llm_budget cache invalidation failed (non-fatal): %s", e)

    return _resolve_config(tool, config)


# =============================================================================
# Legacy compatibility shim — maps to the wizard tool.
# =============================================================================

class _LegacyAIConfigResponse(BaseModel):
    provider: str = "openai"
    model: str = ""
    api_key: bool = False
    base_url: str = ""
    temperature: str = "0.3"
    max_tokens: str = "4096"
    assistant_enabled: bool = False


class _LegacyAIConfigUpdate(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: Optional[str] = None
    max_tokens: Optional[str] = None
    assistant_enabled: Optional[bool] = None


@router.get("/ai-config", response_model=_LegacyAIConfigResponse, deprecated=True)
def legacy_get_ai_config(
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """DEPRECATED: use GET /platform/ai-config/{tool}. Maps to the wizard tool."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")

    raw = _read_tool_config("wizard")
    resolved = _resolve_config("wizard", raw)
    return _LegacyAIConfigResponse(
        provider=resolved.provider or "openai",
        model=resolved.model or "",
        api_key=resolved.api_key,
        base_url=resolved.base_url or "",
        temperature=resolved.temperature,
        max_tokens=resolved.max_tokens,
        assistant_enabled=bool(_read_tool_config("assistant").get("execution_mode", "cloud") != "off"),
    )


@router.put("/ai-config", response_model=_LegacyAIConfigResponse, deprecated=True)
def legacy_update_ai_config(
    update: _LegacyAIConfigUpdate,
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """DEPRECATED: use PUT /platform/ai-config/{tool}. Writes through to the wizard tool."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")

    config = _read_tool_config("wizard")
    if update.provider is not None:
        config["provider"] = update.provider
    if update.model is not None:
        config["model"] = update.model
    if update.api_key is not None:
        if update.api_key == "":
            config.pop("api_key", None)
        else:
            config["api_key"] = update.api_key
    if update.base_url is not None:
        config["base_url"] = update.base_url
    if update.temperature is not None:
        config["temperature"] = update.temperature
    if update.max_tokens is not None:
        config["max_tokens"] = update.max_tokens
    config.setdefault("execution_mode", "local")

    _write_vault_path(_vault_path_for("wizard"), config)
    logger.info("AI config (legacy) updated by user %s", current_user.id)

    resolved = _resolve_config("wizard", config)
    return _LegacyAIConfigResponse(
        provider=resolved.provider or "openai",
        model=resolved.model or "",
        api_key=resolved.api_key,
        base_url=resolved.base_url or "",
        temperature=resolved.temperature,
        max_tokens=resolved.max_tokens,
        assistant_enabled=bool(_read_tool_config("assistant").get("execution_mode", "cloud") != "off"),
    )
