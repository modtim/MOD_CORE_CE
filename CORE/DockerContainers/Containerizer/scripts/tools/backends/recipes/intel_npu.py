"""Intel NPU (Meteor / Lunar / Arrow Lake) recipe via OpenVINO.

Real recipe in Phase 3 — the wizard's NPU tab now produces a Dockerfile
that converts the HuggingFace model to OpenVINO IR at build time, plus
rewritten source that swaps `Bert/Blip/...ForX` for the matching
`OVModelForX` from `optimum.intel.openvino`.

What ships:

    1. `_HF_TO_OV` — a small mapping from HF class names to their
       optimum-intel OVModel* equivalents. Keys come from
       architectures.py's optimum allowlist; values from Optimum's
       exporter docs.
    2. `_extract_model_load(...)` — find `var = HFClass.from_pretrained(
       MODEL_ID, ...)` in the source, returning (var, hf_class,
       model_id, task).
    3. `rewrite_source(...)` — for the detected model class:
       - replace the import (`from transformers import HFClass` →
         `from optimum.intel.openvino import OVClass`)
       - replace the `from_pretrained` call (`HFClass.from_pretrained(
         MODEL_ID)` → `OVClass.from_pretrained("/opt/openvino_model")`)
       - the IR was baked into the image at build time, so the runtime
         loads from disk — no re-export at run time.
    4. `render_dockerfile(...)` — emits the OpenVINO base image, runs
       `optimum-cli export openvino --weight-format int8` at build,
       installs application-level deps. The export command targets a
       fixed path (`/opt/openvino_model`) the rewritten source loads
       from.

Architectures NOT in the optimum allowlist (architectures.py
`_OPTIMUM_OPENVINO_SUPPORTED`) are returned with
`incompatible_reason` set; the planner emits the stub Dockerfile +
the explanation. Calibration data UX (representative inputs for INT8
post-training quant) is deferred to a follow-up wizard step — for
now we use the model's default calibration via `--dataset` parameters
that Optimum can fetch automatically for vision/text classification.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from ..architectures import _OPTIMUM_OPENVINO_SUPPORTED
from ..recipe_interface import (
    FileChange, FileWarning, RecipeOutput, RewrittenFile,
)


# ---------------------------------------------------------------------------
# HF class → OVModel mapping
# ---------------------------------------------------------------------------

# Names of the Optimum-Intel exporter classes that the Dockerfile's
# `optimum-cli export openvino --task ...` step expects to be importable
# at runtime. Keys must match _OPTIMUM_OPENVINO_SUPPORTED.
_HF_TO_OV: Dict[str, Tuple[str, str]] = {
    # Encoder-only / classification
    "BertForSequenceClassification":  ("OVModelForSequenceClassification", "text-classification"),
    "RobertaForSequenceClassification":("OVModelForSequenceClassification", "text-classification"),
    "DistilBertForSequenceClassification": ("OVModelForSequenceClassification", "text-classification"),
    "BertForTokenClassification":      ("OVModelForTokenClassification", "token-classification"),
    "BertForQuestionAnswering":        ("OVModelForQuestionAnswering", "question-answering"),
    "BertForMaskedLM":                 ("OVModelForMaskedLM", "fill-mask"),
    "DebertaV2ForSequenceClassification":("OVModelForSequenceClassification", "text-classification"),
    "BertModel":                       ("OVModelForFeatureExtraction", "feature-extraction"),
    "RobertaModel":                    ("OVModelForFeatureExtraction", "feature-extraction"),
    # Decoder-only LMs
    "GPT2LMHeadModel":      ("OVModelForCausalLM", "text-generation"),
    "GPTNeoForCausalLM":    ("OVModelForCausalLM", "text-generation"),
    "GPTJForCausalLM":      ("OVModelForCausalLM", "text-generation"),
    "LlamaForCausalLM":     ("OVModelForCausalLM", "text-generation"),
    "MistralForCausalLM":   ("OVModelForCausalLM", "text-generation"),
    "MixtralForCausalLM":   ("OVModelForCausalLM", "text-generation"),
    "PhiForCausalLM":       ("OVModelForCausalLM", "text-generation"),
    "Phi3ForCausalLM":      ("OVModelForCausalLM", "text-generation"),
    "Qwen2ForCausalLM":     ("OVModelForCausalLM", "text-generation"),
    "GemmaForCausalLM":     ("OVModelForCausalLM", "text-generation"),
    "Gemma2ForCausalLM":    ("OVModelForCausalLM", "text-generation"),
    "BloomForCausalLM":     ("OVModelForCausalLM", "text-generation"),
    "OPTForCausalLM":       ("OVModelForCausalLM", "text-generation"),
    "FalconForCausalLM":    ("OVModelForCausalLM", "text-generation"),
    "CodeGenForCausalLM":   ("OVModelForCausalLM", "text-generation"),
    "StarCoder2ForCausalLM":("OVModelForCausalLM", "text-generation"),
    # Seq2seq
    "T5ForConditionalGeneration":     ("OVModelForSeq2SeqLM", "text2text-generation"),
    "BartForConditionalGeneration":   ("OVModelForSeq2SeqLM", "text2text-generation"),
    "MBartForConditionalGeneration":  ("OVModelForSeq2SeqLM", "text2text-generation"),
    "MarianMTModel":                  ("OVModelForSeq2SeqLM", "translation"),
    "PegasusForConditionalGeneration":("OVModelForSeq2SeqLM", "summarization"),
    # Vision
    "ViTForImageClassification":      ("OVModelForImageClassification", "image-classification"),
    "BeitForImageClassification":     ("OVModelForImageClassification", "image-classification"),
    "DeiTForImageClassification":     ("OVModelForImageClassification", "image-classification"),
    "SwinForImageClassification":     ("OVModelForImageClassification", "image-classification"),
    "ConvNextForImageClassification": ("OVModelForImageClassification", "image-classification"),
    "ResNetForImageClassification":   ("OVModelForImageClassification", "image-classification"),
    # Vision-language
    "BlipForConditionalGeneration":   ("OVModelForVision2Seq", "image-to-text"),
    "BlipForQuestionAnswering":       ("OVModelForVision2Seq", "image-to-text"),
    "Blip2ForConditionalGeneration":  ("OVModelForVision2Seq", "image-to-text"),
    "VisionEncoderDecoderModel":      ("OVModelForVision2Seq", "image-to-text"),
    "CLIPModel":                      ("OVModelForFeatureExtraction", "feature-extraction"),
    # Audio
    "Wav2Vec2ForCTC":                 ("OVModelForCTC", "automatic-speech-recognition"),
    "WhisperForConditionalGeneration":("OVModelForSpeechSeq2Seq", "automatic-speech-recognition"),
    # Diffusion
    "StableDiffusionPipeline":        ("OVStableDiffusionPipeline", "text-to-image"),
    "StableDiffusionXLPipeline":      ("OVStableDiffusionXLPipeline", "text-to-image"),
}


_FROM_PRETRAINED = re.compile(
    r"(?P<indent>^\s*)(?P<var>\w+)\s*=\s*"
    r"(?P<cls>[A-Z][A-Za-z0-9_]+)"
    r"\.from_pretrained\(\s*"
    r"(?P<arg>[^,)\n]+)",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Dockerfile template
# ---------------------------------------------------------------------------

_BASE_IMAGE = "ghcr.io/modtim/base-npu:openvino-2025"

_OV_MODEL_PATH = "/opt/openvino_model"


_DOCKERFILE_TEMPLATE = """\
FROM {base_image}
LABEL mod.gpu_variant="npu"
LABEL mod.recipe="intel_npu"
LABEL mod.recipe_phase="3"

WORKDIR /data

# OpenVINO + optimum-intel for the NPU plugin. INT8 is mandatory for
# any meaningful NPU perf — Optimum's POT does post-training quant
# at export time using a small built-in calibration dataset.
RUN pip install --no-cache-dir \\
        openvino==2025.0.0 \\
        openvino-tokenizers==2025.0.0 \\
        optimum-intel[openvino,nncf]==1.21.0 \\
        transformers pillow numpy

# Bake the OpenVINO IR into the image at build time. The export
# fetches weights from Hugging Face (build is online), runs
# ONNX-style export, and emits xml/bin files into {ov_path}. The
# rewritten user_script.py loads from this path at runtime.
RUN optimum-cli export openvino \\
        --model {model_id} \\
        --task {task} \\
        --weight-format int8 \\
        {ov_path}

{hero_pip_block}

COPY --chmod=0755 entrypoint.sh /app/entrypoint.sh

ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
"""


# ---------------------------------------------------------------------------
# Source rewriting
# ---------------------------------------------------------------------------

def _extract_model_load(body: str) -> Optional[Tuple[str, str, str]]:
    """Find the first `var = HFClass.from_pretrained(MODEL_ID, ...)`
    matching an HF class on our mapping table. Returns
    (variable_name, hf_class_name, model_id_literal) or None.

    `model_id_literal` is the raw text of the first positional arg
    — could be a string literal, a variable name, or an environment
    lookup. We strip surrounding quotes for string literals.
    """
    for m in _FROM_PRETRAINED.finditer(body):
        cls = m.group("cls")
        if cls in _HF_TO_OV:
            arg = m.group("arg").strip()
            # Unwrap simple string literal
            if (arg.startswith('"') and arg.endswith('"')) or \
               (arg.startswith("'") and arg.endswith("'")):
                arg = arg[1:-1]
            return m.group("var"), cls, arg
    return None


def _rewrite_python_for_ov(body: str, hf_cls: str, var_name: str) -> Tuple[
    str, List[FileChange], List[FileWarning]
]:
    """Apply NPU-specific source rewrites: import swap, class swap,
    from_pretrained-args swap to /opt/openvino_model. Returns
    (rewritten body, change list, warning list)."""
    ov_cls, _task = _HF_TO_OV[hf_cls]
    changes: List[FileChange] = []
    warnings: List[FileWarning] = []
    new_body = body

    # 1. Add the optimum.intel.openvino import.
    if f"from optimum.intel.openvino import {ov_cls}" not in new_body:
        # Insert after the first line that imports anything from
        # transformers (or from torch as a fallback).
        anchor = re.search(
            r"^[ \t]*from\s+transformers\s+import\s+\S+.*$",
            new_body, re.MULTILINE,
        )
        if anchor is None:
            anchor = re.search(
                r"^[ \t]*import\s+(?:[\w]+\s*,\s*)*torch\b.*$",
                new_body, re.MULTILINE,
            )
        if anchor is not None:
            insertion = f"from optimum.intel.openvino import {ov_cls}\n"
            insert_at = anchor.end()
            new_body = (
                new_body[:insert_at] + "\n" + insertion +
                new_body[insert_at:].lstrip("\n")
            )
            changes.append(FileChange(
                before="(no optimum.intel.openvino import)",
                after=insertion.rstrip("\n"),
                line=new_body[:insert_at].count("\n") + 2,
                reason=f"Inserted OVModel import for NPU runtime "
                       f"(maps {hf_cls} → {ov_cls}).",
            ))
        else:
            warnings.append(FileWarning(
                severity="warn",
                message="No transformers/torch import line found to "
                        "anchor the OVModel import; operator must add "
                        "`from optimum.intel.openvino import "
                        f"{ov_cls}` manually.",
            ))

    # 2. Swap `HFClass.from_pretrained(MODEL_ID...)` for
    # `OVClass.from_pretrained("/opt/openvino_model"...)`. We rewrite
    # only the FIRST matching call — multiple model loads in one
    # script are unusual and need operator review.
    pat = re.compile(
        rf"(?m)^(?P<indent>\s*){re.escape(var_name)}\s*=\s*"
        rf"{re.escape(hf_cls)}\.from_pretrained\([^)]*\)"
    )
    m = pat.search(new_body)
    if m is not None:
        new_call = (f"{m.group('indent')}{var_name} = "
                    f"{ov_cls}.from_pretrained(\"{_OV_MODEL_PATH}\")")
        before = m.group(0)
        new_body = new_body[:m.start()] + new_call + new_body[m.end():]
        changes.append(FileChange(
            before=before.split("\n")[0],
            after=new_call,
            line=body[:m.start()].count("\n") + 1,
            reason=f"Rewrote {hf_cls}.from_pretrained(...) → "
                   f"{ov_cls}.from_pretrained({_OV_MODEL_PATH!r}). "
                   f"OpenVINO IR was baked into the image at build "
                   f"time.",
        ))
    else:
        warnings.append(FileWarning(
            severity="warn",
            message=f"Couldn't locate the {hf_cls}.from_pretrained call "
                    f"in the source body — operator must rewrite it "
                    f"manually to use {ov_cls} from "
                    f"'{_OV_MODEL_PATH}'.",
        ))

    # 3. NPU doesn't have torch tensors that target XPU — drop any
    # `.to("cuda")` / `.to("xpu")` calls (they no-op on the OV path).
    cuda_to = re.compile(r"\.to\((\"|')(cuda|xpu)(\"|')\)")
    if cuda_to.search(new_body):
        for lineno, line in enumerate(new_body.splitlines(keepends=True), start=1):
            if cuda_to.search(line):
                rewritten = cuda_to.sub("", line)
                if rewritten != line:
                    changes.append(FileChange(
                        before=line.rstrip("\n"),
                        after=rewritten.rstrip("\n"),
                        line=lineno,
                        reason="Dropped .to('cuda'/'xpu') — OVModel "
                               "executes the entire compute graph on "
                               "the NPU; torch device transfer is a "
                               "no-op on this path.",
                    ))
        new_body = cuda_to.sub("", new_body)

    return new_body, changes, warnings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_dockerfile(
    *,
    hero_pip_packages: List[str] = (),
    model_id: str = "",
    task: str = "feature-extraction",
    **_: object,
) -> str:
    """Render the Intel NPU Dockerfile.

    `model_id` is the HuggingFace repo (e.g. "Salesforce/blip-image-
    captioning-large") or local path the source's first
    `from_pretrained(...)` call refers to. `task` is the Optimum
    exporter task name corresponding to the model's HF class
    (passed in by the planner from `_HF_TO_OV`).

    When `model_id` is empty the generated Dockerfile is non-runnable
    — operator must hand-edit the optimum-cli line. The planner only
    invokes this with a real `model_id` when the source detector
    found one.
    """
    if hero_pip_packages:
        block = "RUN pip install --no-cache-dir " + " ".join(
            _shell_quote(p) for p in hero_pip_packages
        )
    else:
        block = "# (no application-level pip dependencies)"

    rendered = _DOCKERFILE_TEMPLATE.format(
        base_image=_BASE_IMAGE,
        model_id=_shell_quote(model_id or "TODO_MODEL_ID"),
        task=_shell_quote(task or "feature-extraction"),
        ov_path=_OV_MODEL_PATH,
        hero_pip_block=block,
    )

    # Phase 9 — splice in calibration dataset flags when the build
    # environment carries an INLINE_CALIBRATION_CONFIG. A null config
    # leaves the Dockerfile unchanged (Optimum falls back to its
    # default dataset — same as pre-P9 behaviour).
    try:
        from tools.calibration_runner import (
            load_config_from_env, build_calibration_flags, apply_to_optimum_cli,
            CALIB_DIR, ENV_VAR_NAME,  # noqa: F401
        )
        cfg = load_config_from_env()
        flags = build_calibration_flags(cfg)
        if not flags.get("skip"):
            rendered = apply_to_optimum_cli(rendered, flags)
            prelude = flags.get("docker_prelude") or ""
            if prelude:
                # Insert the prelude immediately before the optimum-cli
                # RUN block so the calibration set exists when the
                # exporter looks for it.
                marker = "RUN optimum-cli export openvino"
                if marker in rendered:
                    rendered = rendered.replace(
                        marker, f"{prelude}\n\n{marker}",
                    )
    except Exception:
        logger.warning(
            "P9 calibration flag splice failed; falling back to "
            "Optimum's default dataset", exc_info=True,
        )

    return rendered


def _shell_quote(s: str) -> str:
    if not s:
        return "''"
    if all(c.isalnum() or c in "._-=<>!~+@/:" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


# Scratch the planner reads after rewrite_source(). Holds Dockerfile
# kwargs that depend on what the rewriter detected (model id, task).
# Empty dict for recipes that don't need per-build context.
_last_render_kwargs: Dict[str, str] = {}


def rewrite_source(source_files: Dict[str, str]) -> RecipeOutput:
    """Rewrite source for Intel NPU. Detects the HF class, refuses
    architectures not on the optimum allowlist, otherwise rewrites
    imports + from_pretrained calls + drops device transfers."""
    global _last_render_kwargs
    _last_render_kwargs = {}

    # Find the first model-load expression we recognise across all
    # Python files. We rewrite that file; the rest pass through.
    detection: Optional[Tuple[str, str, str, str]] = None  # (filename, var, cls, model_id)
    for fn, body in source_files.items():
        if not fn.endswith((".py", ".sh")):
            continue
        found = _extract_model_load(body)
        if found is not None:
            var, cls, model_id = found
            detection = (fn, var, cls, model_id)
            break

    if detection is None:
        # Workload-level incompatibility: NPU only makes sense for
        # ML inference using a HuggingFace model. A binary-shell-out
        # script (openMVG, ffmpeg, …) has no `from_pretrained()`
        # to target — there's nothing for `optimum-cli export` to
        # bake into the image and no tensor graph for the NPU runtime
        # to schedule. Set `workload_incompatible=True` so the planner
        # suppresses the stub Dockerfile (the OpenVINO stub with
        # `TODO_MODEL_ID` baked in cannot be hand-ported into a
        # working node for a non-ML workload).
        out_files = {fn: RewrittenFile(body=body) for fn, body in source_files.items()}
        return RecipeOutput(
            files=out_files,
            workload_incompatible=True,
            incompatible_reason=(
                "No HuggingFace model load detected in source — Intel "
                "NPU recipe needs a `from_pretrained()` call on a class "
                "in the optimum-intel allowlist (LLMs, ViT, BLIP, "
                "Whisper, Stable Diffusion, etc.). Operator must hand-"
                "port if this node uses a custom load path."
            ),
        )

    fn, var_name, hf_cls, model_id = detection
    discovered_mapping: Optional[Dict[str, str]] = None
    if hf_cls not in _OPTIMUM_OPENVINO_SUPPORTED:
        # Phase 8 — LLM fallback. Ask the resolver if it knows an
        # OVModelFor* class for this unseen HF class. Only fires when
        # MOD_ARCH_RESOLVER_ENABLED=1; otherwise stays with the
        # incompatible path for deterministic dev loops.
        resolver_hit = None
        try:
            from tools.arch_resolver import resolve_arch, as_arch_mapping_entry
            resolver_hit = resolve_arch(hf_cls, seed_map=_HF_TO_OV)
        except Exception:
            resolver_hit = None
        if resolver_hit is not None:
            ov_class, task = resolver_hit
            # Extend the in-process map so downstream detection /
            # rewrite logic just works without a second code path.
            _HF_TO_OV[hf_cls] = (ov_class, task)
            try:
                discovered_mapping = as_arch_mapping_entry(
                    hf_cls, ov_class, task, confidence=1.0, source="llm",
                )
            except Exception:
                discovered_mapping = None
        else:
            out_files = {f: RewrittenFile(body=b) for f, b in source_files.items()}
            return RecipeOutput(
                files=out_files,
                incompatible_reason=(
                    f"Detected HF class '{hf_cls}' is not on the optimum-"
                    f"intel exporter allowlist. Supported architectures: "
                    f"see backends/architectures.py. Operator can hand-"
                    f"port using `optimum-cli export onnx` instead, or "
                    f"choose a different model class."
                ),
            )

    if not model_id or model_id.startswith("$") or "{" in model_id:
        # Computed/env-derived model id — Dockerfile build can't
        # know the value, so we can't run optimum-cli at build.
        out_files = {f: RewrittenFile(body=b) for f, b in source_files.items()}
        return RecipeOutput(
            files=out_files,
            incompatible_reason=(
                f"Source's model id is computed at runtime "
                f"(`{model_id}`) — Intel NPU build needs the literal "
                f"HuggingFace repo so optimum-cli can export the IR "
                f"at image-build time. Use a hardcoded model id or "
                f"set MODEL_ID at build via a Dockerfile ARG."
            ),
        )

    # Stash Dockerfile kwargs the planner will pass to render_dockerfile().
    _, task = _HF_TO_OV[hf_cls]
    _last_render_kwargs.update({"model_id": model_id, "task": task})

    # Apply the rewrite to the matched file. Other files pass through.
    out_files: Dict[str, RewrittenFile] = {}
    for f, body in source_files.items():
        if f == fn:
            new_body, changes, warnings = _rewrite_python_for_ov(
                body, hf_cls, var_name,
            )
            out_files[f] = RewrittenFile(body=new_body, changes=changes,
                                         warnings=warnings)
        else:
            out_files[f] = RewrittenFile(body=body)
    return RecipeOutput(
        files=out_files,
        incompatible_reason=None,
        discovered_arch_mapping=discovered_mapping,
    )


# Phase-1 callers still call render(...) — keep that name alive.
def render(
    *, image_name: str = "node",
    hero_pip_packages: List[str] = (),
    model_id: str = "",
    task: str = "feature-extraction",
    **_: object,
) -> str:
    return render_dockerfile(
        hero_pip_packages=hero_pip_packages,
        model_id=model_id,
        task=task,
    )
