"""node_definitions endpoints for the `assets_cogs_files` domain.

Split out of the monolithic routers/node_definitions.py during Gate 2a
(openspec: stack-training-and-prewarm). Pure behaviour-preserving MOVE:
paths, methods, dependencies, request/response models and helpers are
unchanged. The endpoints below attach to this sub-module's `router`
(and `external_router`), which the aggregator includes at the same
mount points main.py already uses.
"""

from fastapi import APIRouter
from ._shared import *  # noqa: F401,F403  (helpers, models, imports, constants)

router = APIRouter(
    responses={404: {"description": "Not found"}},
)


def check_syntax_endpoint(
    request: CheckSyntaxRequest,
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Check the submitted script/Dockerfile for syntax errors BEFORE
    the user advances to analysis + build. Python: ast.parse. Bash:
    `bash -n`. Dockerfile: directive validation + required FROM. Other
    interpreters: pass-through (we only check what's cheap)."""
    from utils.syntax_check import check_source
    r = check_source(request.source_type, request.interpreter, request.script)
    return CheckSyntaxResponse(**r)

def analyze_script(
    request: AnalyzeScriptRequest,
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Synchronous LLM call via the wizard tool's Vault config. Returns
    a best-effort analysis the New Node Wizard uses to pre-fill
    Configure / Variables / Network steps. Never raises on LLM failure —
    returns a low-confidence fallback so the wizard keeps moving."""
    from node_builder.analyze_prompt import (
        SYSTEM_PROMPT, build_user_prompt, coerce_and_validate, fallback_response,
    )
    from utils.llm_client import call_wizard_llm, LLMClientError

    if os.getenv("DISABLE_WIZARD_AUTOFILL", "false").lower() == "true":
        return AnalyzeScriptResponse(**fallback_response(
            "Wizard auto-fill is disabled (DISABLE_WIZARD_AUTOFILL=true)."
        ))

    if not (request.script or "").strip():
        return AnalyzeScriptResponse(**fallback_response(
            "No content provided."
        ))

    # Syntax check first. If the user's input doesn't parse, sending
    # it to the LLM is pointless — we'd get hallucinated output that
    # confidently describes broken code. Fast-fail with a low-confidence
    # response that carries the error so the wizard can surface it.
    from utils.syntax_check import check_source
    syntax = check_source(request.source_type, request.interpreter, request.script)
    if not syntax["ok"]:
        fb = fallback_response(
            f"Syntax error{' on line ' + str(syntax['line']) if syntax.get('line') else ''}: {syntax['error']}"
        )
        return AnalyzeScriptResponse(**fb)

    user_prompt = build_user_prompt(
        request.source_type,
        request.script,
        request.interpreter,
        # Per-step Q&A propagation (issue #18). When the wizard re-fires
        # analyze-script after the user resolved a previous turn's
        # clarifying_questions, request.user_answers carries the answers.
        # The prompt includes them in a "Previous user answers" block so
        # the LLM converges to a final analysis (typically with the glue
        # artifact attached + zero remaining questions).
        user_answers=request.user_answers,
    )

    try:
        # 180s because the default wizard model (Devstral ~14 GB) can
        # take 60-120s to cold-load on disk the first time the API
        # process calls it. Keep-alive=-1 means subsequent calls are
        # fast (~10-25s for a trivial reply, longer for a full
        # analysis). Tweakable via ANALYZE_SCRIPT_TIMEOUT_SECONDS.
        timeout_s = int(os.getenv("ANALYZE_SCRIPT_TIMEOUT_SECONDS", "180"))
        raw = call_wizard_llm(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format="json",
            timeout_s=timeout_s,
        )
    except LLMClientError as e:
        logger.info("analyze-script LLM unavailable, returning fallback: %s", e)
        return AnalyzeScriptResponse(**fallback_response(f"Analysis unavailable: {e}"))

    if not isinstance(raw, dict):
        logger.info("analyze-script LLM returned non-dict: %r", type(raw).__name__)
        return AnalyzeScriptResponse(**fallback_response("Analysis returned unexpected shape."))

    analyzed = coerce_and_validate(raw)
    # GPU-library knowledge override. The analyzer LLM's rule is
    # "torch/tensorflow/cupy → CUDA, otherwise CPU." It has no
    # awareness of high-level frameworks (nerfstudio, pytorch3d,
    # gaussian-splatting, vllm, etc.) whose CUDA dependency is
    # indirect. The deterministic library index catches them and
    # surfaces the override as a clarifying question.
    from node_builder.gpu_library_index import apply_override, confirm_accelerator_need
    analyzed = apply_override(analyzed, request.script or "")
    # Second-guess a GPU classification ("are you sure this needs a GPU?").
    # Deterministic fact-check leads (no GPU library/binary => CPU); an LLM
    # re-read backstops the ambiguous case. Catches the analyzer hallucinating
    # GPU for a CPU workload (pypdfium2/PDFium mislabelled 'CUDA'), which with
    # variant fan-out on would build every GPU backend for a CPU task.
    analyzed = confirm_accelerator_need(analyzed, request.script or "")

    # Item #8 — predict build difficulty from tenant-scoped
    # LibraryInstallRecipe success_count (GB.LEAK.5). The legacy global
    # LibraryProfile columns are no longer read.
    # Heuristic:
    #   - Any imported library with successful_build_count >= 5 → easy
    #     (1-2 attempts, 5-10 min); recipe is battle-tested.
    #   - Any imported library with count 2-4 → medium (2-4 attempts).
    #   - Any imported library accelerator-required but no recipe yet
    #     (first-time CUDA workload) → hard (5-10 attempts).
    #   - Plain CPU scripts → easy (1-2 attempts).
    try:
        from database import SessionLocal as _SL
        from node_builder.gpu_library_index import (
            _normalise, extract_package_candidates,
        )
        candidate_libs = [
            _normalise(l) for l in extract_package_candidates(request.script or "")
        ]
        best_count = 0
        worst_signal: Optional[str] = None
        if candidate_libs:
            with _SL() as _s:
                for lib in candidate_libs:
                    row = _s.get(models.LibraryProfile, lib)
                    if row is None:
                        continue
                    accel = (analyzed.get("process_type") or "CPU").lower()
                    # 10.3.s1 — tenant-scoped accel counters via the accessor
                    # (per-tenant LibraryInstallRecipe rows; the legacy global
                    # library_profiles JSONB counters are no longer read).
                    from node_builder.install_recipes import tenant_accel_counts
                    n = tenant_accel_counts(
                        _s, tenant_id=current_user.tenant_id, library_name=lib
                    ).get(accel, 0)
                    if n > best_count:
                        best_count = n
                    # Flag accelerator-required libs without a recipe.
                    if row.gpu_required and row.gpu_required not in ("CPU", "UNKNOWN") and n == 0:
                        worst_signal = f"first-time build for {lib} on {accel}"
        # Surface a prediction even when candidate_libs is empty —
        # `extract_package_candidates` only catches lines that look
        # like `import X` / pip install lines, so scripts that only
        # `import cv2` (sans requirements.txt) yielded NO prediction
        # at all, leaving the Review-step badge blank. Defaulting to
        # the CPU "Plain build" / GPU "First-time" tier is much more
        # informative than silence.
        if best_count >= 5:
            analyzed["predicted_attempts"] = 2
            analyzed["predicted_minutes"] = 8
            analyzed["prediction_basis"] = f"High-confidence recipe ({best_count} prior builds)"
        elif best_count >= 2:
            analyzed["predicted_attempts"] = 3
            analyzed["predicted_minutes"] = 15
            analyzed["prediction_basis"] = f"Medium-confidence recipe ({best_count} prior builds)"
        elif worst_signal:
            analyzed["predicted_attempts"] = 8
            analyzed["predicted_minutes"] = 30
            analyzed["prediction_basis"] = worst_signal
        elif (analyzed.get("process_type") or "CPU") == "CPU":
            analyzed["predicted_attempts"] = 2
            analyzed["predicted_minutes"] = 5
            analyzed["prediction_basis"] = "Plain CPU build"
        else:
            analyzed["predicted_attempts"] = 8
            analyzed["predicted_minutes"] = 30
            analyzed["prediction_basis"] = "First-time accelerator build"
    except Exception:  # noqa: BLE001
        logger.exception("build-time prediction failed — non-fatal")

    # Attach the REAL default value to each env var so the wizard's Review
    # step can show it (the LLM analyzer records `required` but not the
    # default literal, so the UI always fell back to "(no default)").
    # Deterministic regex over the script — matches os.getenv("X","default")
    # / os.environ.get("X","default") with a quoted default.
    try:
        import re as _re
        _defaults: Dict[str, str] = {}
        for _m in _re.finditer(
            r"""os\.(?:getenv|environ\.get)\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']\s*,\s*("[^"]*"|'[^']*')""",
            request.script or "",
        ):
            _defaults.setdefault(_m.group(1), _m.group(2)[1:-1])
        for _ev in (analyzed.get("env_vars") or []):
            if (isinstance(_ev, dict) and _ev.get("name") in _defaults
                    and not _ev.get("default_value")):
                _ev["default_value"] = _defaults[_ev["name"]]
    except Exception:  # noqa: BLE001
        logger.exception("env-var default extraction failed — non-fatal")

    return AnalyzeScriptResponse(**analyzed)

__all__ = [
    'check_syntax_endpoint',
    'analyze_script',
]
