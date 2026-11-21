"""Single source of truth for the wizard-analysis LLM prompt.

Used by:
  - CORE/API/routers/node_definitions.py :: POST /analyze-script (sync path)
  - CORE/DockerContainers/Containerizer/scripts/tools/analyze_node.py
    (async path — mirrored copy kept in lockstep via a drift test).

The prompt asks the model to return a strict JSON object describing
what it inferred about the script/Dockerfile — enough to pre-fill
the New Node Wizard so the user's flow becomes "paste → review &
submit" instead of "paste → manually pick 8 things → submit".
"""
from __future__ import annotations


# Valid enum values the wizard accepts. Kept here (not in the LLM
# prompt as a free-form hint) so admission can post-validate the
# model's output and coerce unknown values to safe defaults.
VALID_INPUT_TYPES = {
    "image", "video", "mesh", "audio", "text", "texture",
    "pointcloud", "pdf", "document", "json", "csv", "binary", "other",
}
VALID_OUTPUT_TYPES = VALID_INPUT_TYPES
VALID_PROCESS_TYPES = {"CPU", "CUDA", "ROCM", "INTEL", "NPU"}
VALID_CONFIDENCE = {"high", "medium", "low"}


SYSTEM_PROMPT = """\
You are analyzing a user-submitted script or Dockerfile that's about to
become a workflow node in a visual computing platform. Your job: infer
the node's runtime contract from the code so the user doesn't have to
manually pick every setting in the New Node Wizard.

Important reality check before you start:
  Nodes do not need to have inputs or outputs. Pure-compute nodes
  (run a calculation, return nothing to the workflow) and pure-side-
  effect nodes (write to S3, trigger an external API, no MOD output)
  are first-class valid node types. Do not pressure the user toward
  declaring inputs or outputs they don't want — when the source
  shows no clear I/O, return `input_type: "other"` and
  `output_type: "other"` and move on. Only ask clarifying questions
  when you can't disambiguate INTENT from the source itself.

Reply with a strict JSON object. No prose, no markdown fences, just
the object. Shape:

{
  "input_type":  the most specific conventional label for the data this
    node consumes. Use a category name (image, video, audio, text, json,
    csv, binary, mesh, texture, pointcloud, pdf, document, other) when
    the node handles a whole family, or a specific format name (ifc,
    dwg, dxf, step, parquet, dicom, fasta, vcf, shp, las, onnx,
    safetensors, hdf5, xlsx, glb, fbx, ...) when it's format-specific.
    Lowercase, no extension prefix (use "pdf", not ".pdf").
  "output_type": same rules as input_type.
  "process_type": one of "CPU" | "CUDA" | "ROCM" | "INTEL" | "NPU",
  "suggested_base_image": one of the curated base image variant_dirs
    listed in the user message (e.g. "python-3.11", "cuda-12.4-python3.11",
    "node-20"). Pick the one whose runtime + language + GPU backend
    matches what the script needs. When unsure, "python-3.11" for
    Python, "node-20" for JavaScript, "alpine-3.19" for shell scripts.
  "suggested_name": 2-5 word human-friendly title for the node,
  "suggested_description": one-sentence summary of what it does,
  "env_vars": [
    { "name": "OPENAI_API_KEY",
      "description": "short human-readable purpose",
      "required": true|false }
  ],
  "allowed_egress": [
    { "host": "api.openai.com",
      "ports": [443],
      "reason": "short explanation of why this host is needed" }
  ],
  "confidence": "high" | "medium" | "low",
  "schema_confidence": "high" | "medium" | "low",
  "reasoning": "one or two short sentences explaining your picks.",
  "clarifying_questions": [
    { "id": "q-stable-key",
      "question": "Question to the user, plain English.",
      "options": ["short option 1", "short option 2", ...],
      "source_location": "filename.py:LINE_NUMBER"
    }
  ],
  "suggested_credits_small": integer credit cost for a SMALL input,
  "suggested_credits_medium": integer credit cost for a MEDIUM input,
  "suggested_credits_large": integer credit cost for a LARGE input,
  "glue_artifact": {
    "artifact_type": "entrypoint.sh" | "Dockerfile.glue" | "glue.sh" | "Containerfile.glue",
    "content": "actual file contents the user can edit",
    "description": "one-line description of what the glue does"
  } | null
}

Rules:

1. process_type: if you see imports for torch, tensorflow, cupy, jax[cuda],
   onnxruntime-gpu, transformers with .cuda() calls, pycuda, triton — pick CUDA.
   If you see tensorflow-rocm or pytorch-rocm — pick ROCM. If you see
   intel_extension_for_pytorch or oneDNN — pick INTEL. If you see openvino
   with NPU backend — pick NPU. Otherwise CPU.

   suggested_base_image: keep it consistent with process_type. CUDA work
   should land on `cuda-12.4-python3.11`. ROCm on `rocm-6.1-python3.11`.
   Intel on `intel-xpu-2025.0`. OpenVINO NPU on `npu-openvino-2025`.
   CPU Python on `python-3.11` (or `python-3.12` if the script uses
   3.12-only syntax). CPU Node on `node-20` or `node-22`. Shell scripts
   on `alpine-3.19`. Pick from the catalog the user message lists —
   never invent a new variant_dir.

2. env_vars: list EVERY os.environ.get / os.getenv / os.environ["X"] /
   settings.X / config.X read you see. Include anything in Dockerfile
   ENV directives too.

   required=true rules (apply each independently):
     - `os.environ["X"]`  → required=true (subscript access KeyErrors
       when the var is missing, crashing the script).
     - `os.environ.get("X")` with NO default  → required=false (the
       script handles the None case itself).
     - `os.environ.get("X", "default")` / `os.getenv("X", "default")`
       → required=false (has a default fallback).
     - `os.getenv("X")` with NO default  → required=false.

   Examples:
     `model = os.environ["MODEL_ID"]`      → {"name": "MODEL_ID", "required": true}
     `token = os.getenv("HF_TOKEN")`       → {"name": "HF_TOKEN", "required": false}
     `key = os.environ.get("API_KEY")`     → {"name": "API_KEY",  "required": false}
     `x = os.getenv("PORT", "8080")`       → {"name": "PORT",     "required": false}

3. allowed_egress: list EVERY external HTTP(s) host the script calls
   out to. Look for: openai / anthropic / google SDK imports; explicit
   requests.get("https://...") / httpx / aiohttp calls; huggingface_hub
   model pulls; urllib requests. Ports default to 443 for HTTPS, 80 for
   HTTP, or whatever the URL specifies. DO NOT include MinIO or any
   internal MOD services — those are reachable automatically.

   Library → host shortcuts. Apply each rule ONLY if that exact module
   appears in a real import statement inside the source you were given.
   Do NOT add a host because a library is "related" or because its
   name shows up in a docstring, comment, or string literal. Only
   actual imports count.

   Specifically:
     - `import transformers` or `from transformers import …` or any
       `BlipProcessor.from_pretrained(...)` / `AutoX.from_pretrained(...)`
       / `pipeline(...)` call → add `huggingface.co`.
       (Do NOT add `api.openai.com` — transformers runs locally.)
     - `import huggingface_hub` / `hf_hub_download` / `snapshot_download`
       → add `huggingface.co`.
     - `import openai` or `from openai import …` → add `api.openai.com`.
       No openai import → no openai egress.
     - `import anthropic` or `from anthropic import …` → add
       `api.anthropic.com`. No anthropic import → no anthropic egress.
     - `from google.cloud import storage|bigquery|…` → add the relevant
       `*.googleapis.com`.
     - `import boto3` / `s3 = boto3.client("s3")` → add `*.amazonaws.com`.
     - `import cohere` → add `api.cohere.ai`.
     - A Dockerfile that RUNs `pip install` without a frozen
       requirements file → add `pypi.org` + `files.pythonhosted.org`.
       Do NOT add these just because `import` statements exist; runtime
       pip-install is the signal, not runtime imports.

   The goal is a TIGHT egress list that matches the actual network
   contract of the script. If you see only `import transformers`, the
   output egress MUST be exactly `[{"host": "huggingface.co", ...}]`
   and nothing else. A user who later needs more hosts can add them
   manually — but they should never have to REMOVE hosts you
   hallucinated.

4. input_type / output_type: infer from what the script reads from
   /data/input/ and writes to /data/output/. If unclear (generic
   file manipulation, text processing), use "other".

5. suggested_name: derive from the script's docstring, main function
   name, or comments. Title-case, no slashes, no special chars.

6. confidence:
   - "high" when you're confident about every field.
   - "medium" when input_type, output_type, or env_vars feel uncertain.
   - "low" when the script is too short/generic/empty to analyse reliably.
     If the code body is empty, only comments, a `pass` stub, or a bare
     `print()` with nothing else — you MUST return "low". Do not return
     "high" for trivial scripts just because the defaults fit; there is
     nothing concrete to be confident about.

6a. schema_confidence — narrower than ``confidence``: how sure are you
    SPECIFICALLY about input_type and output_type?
    - "high" when the source either reads from /data/input/ + writes
      to /data/output/, OR has explicit type hints / docstring
      declarations / argparse setup that names the kinds.
    - "medium" when you can guess from filenames, library imports
      (e.g. ``opencv-python`` → image), or function signatures, but
      the code doesn't explicitly say.
    - "low" when the script is generic / abstract / too short to
      tell what it actually consumes or produces. The wizard's
      hard schema gate blocks the user from advancing past the
      Configure step in this case until they confirm the I/O
      manually — better to ask than to ship a node nobody can
      actually wire into a workflow.

7. Keep reasoning to 1-2 short sentences. Don't re-explain the rules.

8. clarifying_questions: include ONLY when you can't disambiguate
   intent from the source alone AND a wrong guess would produce a
   non-composable node.

   Emit a question when:
     - The script reads from a NON-STANDARD location (env var,
       hardcoded path, positional arg) AND it's ambiguous whether
       that's a node INPUT (a file the workflow hands to this
       node) or INTERNAL CONFIG (a value the user already has
       set, like an API key or feature flag).
       Example: `os.environ.get("TARGET_ENTITY")` → ask
       "Is TARGET_ENTITY a file the workflow hands to this node,
       or an internal config value the user already has set?"
     - The script writes to a hardcoded extensionless path AND
       it's ambiguous whether that's a node OUTPUT (passed to
       downstream nodes) or a TEMP file (cleaned up after run).
       Example: `result_blob` → ask "Is result_blob the file
       this node should produce as output, or a temporary file?"
     - The source uses TWO mutually-exclusive GPU backends that
       look co-resident (e.g. both `import torch` AND
       `import intel_extension_for_pytorch`). Ask which one is
       canonical for this build.

   Do NOT emit questions when:
     - The script clearly reads from /data/input/ or writes to
       /data/output/ — the convention answers itself.
     - The body is empty / pure-comment / `pass` / bare `print()`
       — return confidence=low instead and let the user fill the
       fields manually.
     - The answer would not change ANY of input_type / output_type
       / glue_artifact / process_type. Don't ask for free
       information.

   Hard limits:
     - At most 3 questions per analysis (the Q&A panel gets
       cluttered fast, and 3 is enough for the realistic ambiguity
       cases above).
     - `id` is a stable, kebab-case slug the user will not see.
       Re-use the same id if the user previously answered the
       question and you're emitting it again on a follow-up turn
       (you generally shouldn't — once answered, the question is
       done).
     - `options` is the small set of preset choices the user can
       click. Empty array means free-text answer. Most questions
       should HAVE options because plain English answers from
       users are noisy and your follow-up reasoning is brittle.
     - `source_location` (optional) is "<filename>:<line>" so the
       wizard can highlight where the question is about. Omit if
       the question is about the source as a whole.

   When the user message contains "Previous user answers", the
   user has already resolved one or more questions on a prior
   turn. INCORPORATE the answers into your analysis and DO NOT
   re-ask the same question. Emit zero or fewer clarifying
   questions on each follow-up turn (you should converge).

9. glue_artifact: include ONLY when the user's source has a
   confirmed-input or confirmed-output that uses NON-MOD
   conventions, and you can write a small wrapper that bridges
   to /data/input/ + /data/output/ without modifying the user's
   code.

   First turn (no Previous user answers in user message):
     - If the source UNAMBIGUOUSLY reads from a non-standard
       location AND the convention makes the intent clear (e.g.
       `import sys; with open(sys.argv[1]) as f:` → first arg
       is a file path), you MAY emit the glue without asking.
     - If ambiguous, emit a clarifying_question instead and set
       glue_artifact: null on the first turn. Generate the glue
       on the follow-up turn after the user resolves the
       ambiguity.

   Follow-up turn (Previous user answers populated):
     - When the user confirmed an env-var / arg / path is a node
       input or output, emit the glue. Use:
         • artifact_type "entrypoint.sh" for script source
         • artifact_type "Dockerfile.glue" for Dockerfile source
         • artifact_type "glue.sh" for git repo source
         • artifact_type "Containerfile.glue" for raw image source
     - When the user said "no input" / "internal config", DO NOT
       emit a glue — return glue_artifact: null. Pure-compute and
       pure-config nodes don't need a glue.

   The glue MUST be small and obviously safe. Bash for entrypoint
   wrappers, a 1-2 line FROM/COPY/ENTRYPOINT block for Dockerfile
   glue, etc. Do not reach into the user's source — wrap it.

   description is one short sentence that names the bridge:
   "Translates TARGET_ENTITY env var to /data/input/ first file"
   or "Wraps the existing image's CMD to read /data/input/ first".

10. suggested_credits_small / suggested_credits_medium / suggested_credits_large
    — per input-SIZE-tier credit cost estimates (sz-tcp.4, tiered credit
    pricing). The platform charges 1 credit per 30 CPU-seconds OR 6 GPU-
    seconds of compute. Return NON-NEGATIVE INTEGERS for each tier that
    reflect the expected p95 runtime of this node at that input size:
      - small input  (~1024x1024 image / 10k-triangle mesh / 1 MB JSON)
      - medium input (~4096x4096 / 500k tris / 50 MB)
      - large input  (~8192x8192 / 5M tris / 500 MB)
    The tiers are byte-size boundaries (Small <= 2 MB, Medium <= 30 MB,
    Large > 30 MB) — see `services/cost_estimator.get_tier_thresholds()`
    for the authoritative ceilings, so quote them from there rather than
    hardcoding. Costs MUST be non-decreasing across tiers
    (small <= medium <= large). If you genuinely cannot estimate cost for
    a tier, omit that key (admission falls back to the rule floors).

Remember: reply is JSON only. Do not include markdown fences.
"""


def _coerce_credits(value):
    """Normalise one LLM credit suggestion to a non-negative int.

    sz-tcp.4 (tiered credit pricing). Tolerates missing / non-numeric /
    bool / negative / garbage values — all of which become None so the
    wizard leaves that tier blank and admission falls back to the rule
    floors (`services/cost_estimator.RULE_FLOORS`) instead of shipping a
    garbage cost. Never raises.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _tier_credits_from_raw(r: dict) -> dict:
    """Parse the three per-size-tier credit suggestions out of the LLM's
    raw JSON, plus the legacy single-field fallback.

    sz-tcp.4: `suggested_credits_small/medium/large`. When the model only
    emits the legacy single cost (`suggested_credits`, or the node's
    single-value `cost_in_credits` basis), `medium` falls back to it —
    the platform's own invariant is that a node's single cost equals its
    medium-tier cost (see the ck_node_def_cost_in_credits_matches_medium
    check in models.py). Negative / garbage / missing → None.
    """
    out = {
        "suggested_credits_small": _coerce_credits(r.get("suggested_credits_small")),
        "suggested_credits_medium": _coerce_credits(r.get("suggested_credits_medium")),
        "suggested_credits_large": _coerce_credits(r.get("suggested_credits_large")),
    }
    if out["suggested_credits_medium"] is None:
        legacy = None
        for key in ("suggested_credits", "cost_in_credits"):
            legacy = _coerce_credits(r.get(key))
            if legacy is not None:
                break
        out["suggested_credits_medium"] = legacy
    return out


def _curated_base_list_markdown() -> str:
    """Enumerate the curated base-image catalog in a compact form the LLM
    can copy a `variant_dir` out of. Lazy import so this module stays
    usable standalone."""
    try:
        from utils.curated_base_images import CURATED_BASE_IMAGES
    except Exception:
        return ""
    lines = []
    for b in CURATED_BASE_IMAGES:
        lines.append(
            f"- `{b.variant_dir}` — {b.display_name} "
            f"({b.category}, default interpreter: {b.default_interpreter or 'n/a'}, "
            f"compute: {b.process_type_hint})"
        )
    return "\n".join(lines)


def build_user_prompt(
    source_type: str,
    content: str,
    interpreter: str = "python3",
    user_answers: list | None = None,
) -> str:
    """Assemble the user-side prompt. source_type is one of
    'script' / 'dockerfile' / 'repo'. For repo mode the caller passes
    a concatenation of sentinel files (requirements.txt, package.json,
    a representative source file) under `content`.

    `user_answers` (issue #18) is populated on follow-up turns when
    the user has already resolved one or more clarifying_questions
    from a previous response. Each entry is `{"id": str, "answer": str}`.
    The prompt includes a "Previous user answers" block that the LLM
    incorporates into its analysis, typically converging the next
    response on a glue_artifact + zero remaining questions.
    """
    # Clip absurdly long inputs so we don't blow the context window.
    # 12k chars is enough to characterise most scripts and leaves
    # room for the system prompt + response. For long files we keep
    # both the head (imports, top-level config) AND the tail (main
    # entrypoint, .cuda() calls, last-few-lines signal) — truncating
    # the middle is safer than dropping either end because intent-
    # revealing code is usually at the boundaries, not the middle.
    MAX = 12_000
    body = content or ""
    if len(body) > MAX:
        head_n = int(MAX * 0.6)
        tail_n = MAX - head_n
        head = body[:head_n]
        tail = body[-tail_n:]
        body = (
            f"{head}\n\n"
            f"... (truncated {len(content) - MAX} chars from the middle; "
            f"original {len(content)} chars) ...\n\n"
            f"{tail}"
        )

    if source_type == "dockerfile":
        header = "The user submitted the following Dockerfile:"
    elif source_type == "repo":
        header = (
            "The user submitted a git repo URL. Below is a concatenation "
            "of the key manifest + source files found at the repo root:"
        )
    else:
        header = f"The user submitted the following {interpreter} script:"

    catalog = _curated_base_list_markdown()
    catalog_section = (
        f"\n\nCurated base images (`suggested_base_image` MUST be one of these variant_dirs):\n{catalog}\n"
        if catalog else ""
    )

    # Per-step Q&A propagation (issue #18). On follow-up turns the
    # user has already answered one or more questions from the prior
    # response — surface those answers verbatim so the LLM
    # incorporates them and (typically) returns a final analysis with
    # zero remaining clarifying_questions.
    answers_section = ""
    if user_answers:
        rendered = []
        for entry in user_answers:
            if not isinstance(entry, dict):
                continue
            qid = str(entry.get("id") or "").strip()
            answer = str(entry.get("answer") or "").strip()
            if not qid or not answer:
                continue
            rendered.append(f"- [{qid}] {answer}")
        if rendered:
            answers_section = (
                "\n\nPrevious user answers (incorporate these into your "
                "analysis — do NOT re-ask the same questions):\n"
                + "\n".join(rendered)
                + "\n"
            )

    return (
        f"{header}\n\n```\n{body}\n```{catalog_section}{answers_section}\n"
        f"Analyse and return the JSON."
    )


def coerce_and_validate(raw: dict) -> dict:
    """Post-process the LLM's raw JSON output into a safe, schema-
    compliant dict. Unknown enum values get coerced to safe defaults;
    missing fields get filled with conservative defaults. NEVER raises
    — the wizard is allowed to show fallback data, not an error page."""
    r = raw if isinstance(raw, dict) else {}

    def _enum(v, valid, default):
        return v if isinstance(v, str) and v in valid else default

    def _open_string(v, default):
        """Pass-through normalizer for fields where the LLM is allowed
        to return arbitrary domain-specific labels (input_type,
        output_type). Lower-cases + strips; falls back to the default
        only when the value is missing or non-string. Replaces _enum
        for those two fields so labels like 'ifc', 'parquet', 'dicom'
        survive instead of being coerced to 'other'.
        """
        if not isinstance(v, str):
            return default
        cleaned = v.strip().lower()
        return cleaned or default

    # Some models lower-case CUDA etc.; normalise before checking.
    raw_proc = r.get("process_type") or ""
    if isinstance(raw_proc, str):
        raw_proc = raw_proc.upper()

    env_vars = r.get("env_vars") or []
    if not isinstance(env_vars, list):
        env_vars = []
    env_vars = [
        {
            "name": str(e.get("name") or "")[:200],
            "description": str(e.get("description") or "")[:500],
            "required": bool(e.get("required", False)),
        }
        for e in env_vars
        if isinstance(e, dict) and e.get("name")
    ]

    allowed_egress = r.get("allowed_egress") or []
    if not isinstance(allowed_egress, list):
        allowed_egress = []
    cleaned_egress = []
    for e in allowed_egress:
        if not isinstance(e, dict) or not e.get("host"):
            continue
        ports = e.get("ports") or [443]
        if not isinstance(ports, list):
            ports = [443]
        ports = [int(p) for p in ports if isinstance(p, (int, str)) and str(p).isdigit()]
        if not ports:
            ports = [443]
        cleaned_egress.append({
            "host": str(e.get("host"))[:200],
            "ports": ports[:10],
            "reason": str(e.get("reason") or "")[:500],
        })

    # suggested_base_image — must be a known variant_dir from the catalog
    # OR one of the sensible fallbacks matching the process_type. If the
    # LLM hallucinates a variant, we map to the closest safe default so
    # the wizard always has a valid preselect.
    try:
        from utils.curated_base_images import CURATED_BASE_IMAGES
        valid_variants = {b.variant_dir for b in CURATED_BASE_IMAGES}
    except Exception:
        valid_variants = set()

    raw_base = (r.get("suggested_base_image") or "").strip()
    process_fallback_map = {
        "CUDA": "cuda-12.4-python3.11",
        "ROCM": "rocm-6.1-python3.11",
        "INTEL": "intel-xpu-2025.0",
        "NPU": "npu-openvino-2025",
        "CPU": "python-3.11",
    }
    default_base = process_fallback_map.get(
        _enum(raw_proc, VALID_PROCESS_TYPES, "CPU"),
        "python-3.11",
    )
    suggested_base = raw_base if raw_base in valid_variants else default_base

    # Per-step Q&A scaffolding (issue #18). Both fields default to
    # empty / null so a model that doesn't emit them produces the
    # same response shape the wizard had before. Validation here
    # caps list lengths + clamps strings + filters malformed
    # entries so a hallucinated payload doesn't crash the FE.
    raw_questions = r.get("clarifying_questions") or []
    if not isinstance(raw_questions, list):
        raw_questions = []
    cleaned_questions = []
    seen_ids = set()
    for q in raw_questions:
        if not isinstance(q, dict):
            continue
        qid = str(q.get("id") or "").strip()
        question = str(q.get("question") or "").strip()
        if not qid or not question:
            continue
        # Dedupe on id (the LLM occasionally repeats itself).
        if qid in seen_ids:
            continue
        seen_ids.add(qid)
        opts_raw = q.get("options") or []
        if not isinstance(opts_raw, list):
            opts_raw = []
        options = [str(o)[:120] for o in opts_raw if isinstance(o, (str, int, float))][:6]
        cleaned_questions.append({
            "id": qid[:80],
            "question": question[:500],
            "options": options,
            "source_location": str(q.get("source_location") or "")[:200],
        })
    # Hard cap matches the prompt rule (3 questions max).
    cleaned_questions = cleaned_questions[:3]

    raw_glue = r.get("glue_artifact")
    cleaned_glue = None
    if isinstance(raw_glue, dict):
        artifact_type = str(raw_glue.get("artifact_type") or "").strip()
        content = str(raw_glue.get("content") or "")
        # Recognise only the four canonical filename slots; anything
        # else gets rejected so the FE doesn't have to handle a
        # surprise variant.
        valid_types = {
            "entrypoint.sh", "Dockerfile.glue", "glue.sh", "Containerfile.glue",
        }
        if artifact_type in valid_types and content:
            cleaned_glue = {
                "artifact_type": artifact_type,
                "content": content[:8_000],
                "description": str(raw_glue.get("description") or "")[:300],
            }

    tier_credits = _tier_credits_from_raw(r)

    return {
        "input_type": _open_string(r.get("input_type"), "other"),
        "output_type": _open_string(r.get("output_type"), "other"),
        "process_type": _enum(raw_proc, VALID_PROCESS_TYPES, "CPU"),
        "suggested_base_image": suggested_base,
        "suggested_name": str(r.get("suggested_name") or "")[:80],
        "suggested_description": str(r.get("suggested_description") or "")[:400],
        "env_vars": env_vars[:20],
        "allowed_egress": cleaned_egress[:20],
        "confidence": _enum(r.get("confidence"), VALID_CONFIDENCE, "low"),
        "schema_confidence": _enum(
            r.get("schema_confidence"), VALID_CONFIDENCE,
            # Default to overall confidence when LLM didn't break it
            # out (older prompts, off-prompt fallback). Maintains
            # back-compat without weakening the gate.
            _enum(r.get("confidence"), VALID_CONFIDENCE, "low"),
        ),
        "reasoning": str(r.get("reasoning") or "")[:500],
        "clarifying_questions": cleaned_questions,
        "glue_artifact": cleaned_glue,
        **tier_credits,
    }


def fallback_response(reason: str = "") -> dict:
    """Conservative defaults used when the LLM call fails. Wizard
    surfaces these as 'low confidence — please review each field'."""
    return {
        "input_type": "other",
        "output_type": "other",
        "process_type": "CPU",
        "suggested_base_image": "python-3.11",
        "suggested_name": "",
        "suggested_description": "",
        "env_vars": [],
        "allowed_egress": [],
        "confidence": "low",
        "schema_confidence": "low",
        "reasoning": reason or "LLM analysis unavailable — please fill in each field manually.",
        "clarifying_questions": [],
        "glue_artifact": None,
        "suggested_credits_small": None,
        "suggested_credits_medium": None,
        "suggested_credits_large": None,
    }
