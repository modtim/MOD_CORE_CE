"""Per-backend HuggingFace architecture allowlist.

NPU and Intel XPU both have constraints on which transformers
architectures they can run. The single fixed source of truth here is
`optimum-intel`'s exporter list (for OpenVINO/NPU) and IPEX's tested-
matrix doc (for Intel XPU). Architectures that fall outside these lists
are refused up-front with a "this model class isn't on the allowlist"
reason — better than emitting a Dockerfile that fails at convert time
or runs at 1/10th speed.

The allowlists are intentionally conservative: missing a supported
architecture costs us a port we could've done; including an unsupported
one ships a broken image. Phase 2+ CI sweeps refresh these from real
runs against `optimum-cli export openvino` / `ipex.optimize()`.
"""

from __future__ import annotations

import re
from typing import Set


# Architectures `optimum-intel` exports cleanly to OpenVINO IR (as of
# `optimum-intel` 1.21, OpenVINO 2025.0). Anything not on this list
# either has unsupported ops or hasn't been wired into the Optimum
# exporter yet. Source: `optimum/intel/openvino/configuration.py`'s
# `_SUPPORTED_ARCHITECTURES`.
_OPTIMUM_OPENVINO_SUPPORTED: Set[str] = frozenset({
    # Encoder-only / classification
    "BertModel", "BertForSequenceClassification", "BertForTokenClassification",
    "BertForQuestionAnswering", "BertForMaskedLM",
    "RobertaModel", "RobertaForSequenceClassification", "RobertaForTokenClassification",
    "DistilBertModel", "DistilBertForSequenceClassification",
    "AlbertModel", "AlbertForSequenceClassification",
    "XLMRobertaModel", "XLMRobertaForSequenceClassification",
    "DebertaV2ForSequenceClassification", "DebertaV2ForTokenClassification",
    "MPNetForSequenceClassification",
    # Decoder-only LMs
    "GPT2LMHeadModel", "GPTNeoForCausalLM", "GPTJForCausalLM",
    "LlamaForCausalLM", "MistralForCausalLM", "MixtralForCausalLM",
    "PhiForCausalLM", "Phi3ForCausalLM",
    "Qwen2ForCausalLM", "GemmaForCausalLM", "Gemma2ForCausalLM",
    "BloomForCausalLM", "OPTForCausalLM", "FalconForCausalLM",
    "CodeGenForCausalLM", "StarCoder2ForCausalLM",
    # Encoder-decoder / seq2seq
    "T5ForConditionalGeneration", "BartForConditionalGeneration",
    "MBartForConditionalGeneration", "MarianMTModel",
    "PegasusForConditionalGeneration", "BlenderbotForConditionalGeneration",
    # Vision
    "ViTModel", "ViTForImageClassification",
    "BeitForImageClassification", "DeiTForImageClassification",
    "SwinForImageClassification", "ConvNextForImageClassification",
    "ResNetForImageClassification",
    # Vision-language
    "BlipForConditionalGeneration", "BlipForQuestionAnswering",
    "Blip2ForConditionalGeneration",
    "CLIPModel", "CLIPVisionModel", "CLIPTextModel",
    "VisionEncoderDecoderModel",
    # Audio
    "Wav2Vec2ForCTC", "Wav2Vec2ForSequenceClassification",
    "WhisperForConditionalGeneration", "WhisperModel",
    # Speech-to-text
    "Speech2TextForConditionalGeneration",
    # Diffusion (via optimum.intel.openvino.OVDiffusionPipeline)
    "StableDiffusionPipeline", "StableDiffusionXLPipeline",
    "LatentConsistencyModelPipeline",
})


# Architectures IPEX has tested + optimised. Source: IPEX 2.6 doc
# "Optimized Models" + transformers integration matrix. IPEX is more
# permissive than Optimum-OV — most HF inference works because the
# device-string rewrite is enough for ipex.optimize() to do its
# pattern-matching. We still maintain an allowlist to set expectations
# (perf is only documented for these classes).
_IPEX_TUNED_ARCHITECTURES: Set[str] = frozenset({
    # IPEX explicitly tunes these for XPU; everything outside still runs
    # but at unverified perf.
    "LlamaForCausalLM", "MistralForCausalLM", "MixtralForCausalLM",
    "Qwen2ForCausalLM", "GemmaForCausalLM",
    "GPT2LMHeadModel", "OPTForCausalLM", "FalconForCausalLM",
    "BertModel", "RobertaModel",
    "T5ForConditionalGeneration",
    "ViTModel", "ViTForImageClassification",
    "WhisperForConditionalGeneration",
    "BlipForConditionalGeneration", "Blip2ForConditionalGeneration",
    "CLIPModel",
    "StableDiffusionPipeline", "StableDiffusionXLPipeline",
})


IS_ALLOWED = {
    "cuda": None,        # no allowlist — CUDA is the reference
    "rocm": None,        # ROCm runs ~everything CUDA does via API parity
    "cpu":  None,        # CPU runs everything
    # Intel uses IPEX which the comment on _IPEX_TUNED_ARCHITECTURES
    # explicitly notes is "more permissive than Optimum-OV — most HF
    # inference works because the device-string rewrite is enough for
    # ipex.optimize() to do its pattern-matching." Treating the
    # tuned-perf list as a hard compatibility allowlist mis-blocks
    # entire categories of working inference (e.g. AutoModelFor-
    # ImageSegmentation, BiRefNet, DINOv3) for Intel and tanks the
    # auto-port success rate way below the 95-98% target. The list
    # remains in the module for any future caller that wants to read
    # "is this perf-tuned?" advisory data, but it no longer gates
    # compatibility — `is_allowed("intel", ...)` returns True for any
    # HF class.
    "intel": None,
    # NPU stays strict — Optimum-OV cannot export classes outside its
    # allowlist (the OpenVINO IR converter needs the HF arch's
    # config.json + model class to be one it knows about), so the
    # gate here is genuine, not advisory.
    "npu":   _OPTIMUM_OPENVINO_SUPPORTED,
}


# Regex extracted from imports / model.from_pretrained calls. The
# wizard's source detector parses this, not the full Python AST — we're
# looking for `from transformers import X` / `X.from_pretrained(...)`
# patterns to guess what architecture the user is building against.
_HF_CLASS_PATTERN = re.compile(
    r"\b(?:from\s+transformers(?:\.[a-zA-Z0-9_]+)*\s+import\s+([A-Za-z0-9_,\s]+)|"
    r"\btransformers\.([A-Za-z0-9_]+))\b",
)


def detect_hf_classes(source_text: str) -> Set[str]:
    """Best-effort extraction of transformers classes referenced in the
    source. Returns the set of class names; the caller can intersect
    with each backend's allowlist.

    A class isn't required to be present — many simple inference scripts
    use just `AutoModel*` which is in nobody's allowlist by name (it
    resolves to a real architecture at runtime). For those, the wizard
    falls back to "best-effort" classification.
    """
    seen: Set[str] = set()
    for m in _HF_CLASS_PATTERN.finditer(source_text):
        if m.group(1):
            for piece in m.group(1).split(","):
                name = piece.strip().split(" as ")[0].strip()
                if name:
                    seen.add(name)
        if m.group(2):
            seen.add(m.group(2))
    # Filter to capitalised class-like names; drop helpers like
    # "AutoTokenizer", "pipeline", etc. that aren't model classes.
    return {n for n in seen if n[:1].isupper() and "Model" in n
            or n.endswith(("ForCausalLM", "ForConditionalGeneration",
                           "ForSequenceClassification",
                           "ForTokenClassification", "ForQuestionAnswering",
                           "ForMaskedLM", "ForCTC", "ForImageClassification",
                           "Pipeline"))}


def is_allowed(backend: str, architectures: Set[str]) -> bool:
    """True iff every detected architecture is on the backend's allowlist
    (or the backend has no allowlist, i.e. permissive). When the source
    has no detected HF class, we fall through to True — the caller has
    no signal to refuse on. Backend-specific gates (e.g. NPU op-set
    coverage) catch those at convert time downstream.
    """
    allowed = IS_ALLOWED.get(backend)
    if allowed is None:
        return True
    if not architectures:
        return True
    return architectures.issubset(allowed)


def unsupported_subset(backend: str, architectures: Set[str]) -> Set[str]:
    """Return the architectures NOT on the backend's allowlist (caller
    can stuff this in the incompatibility reason)."""
    allowed = IS_ALLOWED.get(backend)
    if allowed is None or not architectures:
        return set()
    return architectures - allowed
