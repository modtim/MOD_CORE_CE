"""Wizard-analysis tool.

Mirrors `CORE/API/node_builder/analyze_prompt.py` so the async
containerizer path and the synchronous API path produce identical
structured output from identical inputs. If you edit the prompt or
the coerce logic here, mirror the change there (and vice versa) —
there's a drift test in `CORE/API/tests/test_analyze_script.py`.

Two entry points on this module:
  - `analyze_node_tool(...)` — async, uses the containerizer's
    shared `llm_providers.get_provider()` plumbing.
  - `coerce_and_validate(dict)` — pure function, same contract as
    the API-side copy.
"""
import json
from typing import Dict, Any, List, Optional

from .llm_providers import get_provider


# Mirror of node_builder/analyze_prompt.py in the API. Kept in lockstep by
# CORE/API/tests/test_analyze_prompt_drift.py — change both copies in the
# same commit, and remember this one only takes effect once the
# Containerizer image is rebuilt.
VALID_INPUT_TYPES = {
    "image", "video", "mesh", "audio", "text", "texture",
    "pointcloud", "pdf", "document", "json", "csv", "binary", "other",
}
VALID_OUTPUT_TYPES = VALID_INPUT_TYPES
VALID_PROCESS_TYPES = {"CPU", "CUDA", "ROCM", "INTEL", "NPU"}
VALID_CONFIDENCE = {"high", "medium", "low"}


SYSTEM_PROMPT = """\
You are analysing a user-submitted script, Dockerfile, or git repo
that's about to become a workflow node in a visual computing
platform. Your job: infer the node's runtime contract from the code
so the user doesn't have to manually pick every setting.

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
  "input_type":  one of "image" | "video" | "mesh" | "audio" | "text" | "texture" | "other",
  "output_type": one of the same values,
  "process_type": one of "CPU" | "CUDA" | "ROCM" | "INTEL" | "NPU",
  "suggested_name": 2-5 word human-friendly title,
  "suggested_description": one-sentence summary,
  "env_vars": [ { "name": str, "description": str, "required": bool } ],
  "allowed_egress": [ { "host": str, "ports": [int, ...], "reason": str } ],
  "confidence": "high" | "medium" | "low",
  "reasoning": "1-2 short sentences.",
  "clarifying_questions": [
    { "id": "q-stable-key",
      "question": "Question to the user.",
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

1. process_type: torch/tensorflow/cupy/jax-cuda/onnxruntime-gpu/pycuda/triton → CUDA.
   tensorflow-rocm/pytorch-rocm → ROCM. intel_extension_for_pytorch/oneDNN → INTEL.
   openvino w/ NPU backend → NPU. Otherwise CPU.

2. env_vars: EVERY os.environ.get / os.getenv / os.environ["X"] / settings.X /
   config.X read. required=true only when called without a default. Include
   Dockerfile ENV declarations too.

3. allowed_egress: EVERY external HTTP(s) host. openai/anthropic/google SDK;
   requests.get / httpx / aiohttp / urllib calls; huggingface_hub pulls.
   Ports default 443. Do NOT include MinIO or internal MOD services.

4. input_type / output_type: infer from /data/input/ reads and /data/output/
   writes. Unclear → "other".

5. suggested_name: derive from docstring, main func, or comments. Title-case.

6. confidence: high when confident on every field; medium when some fields
   uncertain; low when too short/generic/empty to analyse reliably. Empty
   bodies, comment-only files, `pass` stubs, or bare `print()`s MUST be
   "low" — do not return "high" for trivial scripts just because the
   defaults fit.

7. Reply is JSON only. No markdown fences.

8. clarifying_questions: include ONLY when you can't disambiguate intent
   from the source AND a wrong guess would produce a non-composable node.

   Emit when:
     - The script reads from a non-standard location (env var, hardcoded
       path, positional arg) AND it's ambiguous whether that's a node
       input or internal config.
     - The script writes to a hardcoded extensionless path AND it's
       ambiguous whether that's a node output or a temp file.
     - The source uses two mutually-exclusive GPU backends co-resident.

   Do NOT emit when:
     - The script clearly reads from /data/input/ or writes to
       /data/output/ — convention answers itself.
     - The body is empty / pure-comment / `pass` / bare `print()` —
       return confidence=low and let the user fill fields manually.
     - The answer wouldn't change input_type, output_type, glue_artifact,
       or process_type. Don't ask for free information.

   Hard limits: at most 3 questions per analysis. `id` is a stable,
   kebab-case slug. `options` is the small set of preset choices the
   user can click; empty array means free-text. Most questions should
   HAVE options. `source_location` (optional) is "<filename>:<line>".

   When the user message contains "Previous user answers", incorporate
   them and DO NOT re-ask the same question. Each follow-up turn should
   converge — emit zero or fewer questions per turn.

9. glue_artifact: include ONLY when the user has a confirmed-input or
   confirmed-output that uses non-MOD conventions, and you can write a
   small wrapper that bridges to /data/input/ + /data/output/ without
   modifying the user's code.

   First turn (no Previous user answers):
     - If the source unambiguously reads from a non-standard location AND
       intent is clear (e.g. `with open(sys.argv[1]) as f:` → first arg
       is a file path), you MAY emit the glue without asking.
     - If ambiguous, emit a clarifying_question and set glue_artifact: null.
       Generate the glue on the follow-up turn after the user resolves.

   Follow-up turn (Previous user answers populated):
     - When the user confirmed an env-var / arg / path is a node input
       or output, emit the glue. Use:
         • artifact_type "entrypoint.sh" for script source
         • artifact_type "Dockerfile.glue" for Dockerfile source
         • artifact_type "glue.sh" for git repo source
         • artifact_type "Containerfile.glue" for raw image source
     - When the user said "no input" / "internal config", DO NOT emit a
       glue — return glue_artifact: null. Pure-compute and pure-config
       nodes don't need a glue.

   The glue MUST be small and obviously safe. Bash for entrypoint
   wrappers, a 1-2 line FROM/COPY/ENTRYPOINT block for Dockerfile glue.
   Do not reach into the user's source — wrap it.

   description is one short sentence: "Translates TARGET_ENTITY env var
   to /data/input/ first file" or similar.

10. suggested_credits_small / suggested_credits_medium / suggested_credits_large
    — per input-SIZE-tier credit cost estimates (tiered credit pricing).
    The platform charges 1 credit per 30 CPU-seconds OR 6 GPU-seconds of
    compute. Return NON-NEGATIVE INTEGERS for each tier reflecting the
    expected p95 runtime at that input size:
      - small input  (~1024x1024 image / 10k-triangle mesh / 1 MB JSON)
      - medium input (~4096x4096 / 500k tris / 50 MB)
      - large input  (~8192x8192 / 5M tris / 500 MB)
    The tiers are byte-size boundaries (Small <= 2 MB, Medium <= 30 MB,
    Large > 30 MB). Costs MUST be non-decreasing across tiers. Omit a key
    only when you genuinely cannot estimate that tier.
"""


def _user_prompt(
    source_type: str,
    content: str,
    interpreter: str = "python3",
    user_answers: Optional[List[Dict[str, Any]]] = None,
) -> str:
    MAX = 12_000
    body = content or ""
    if len(body) > MAX:
        head_n = int(MAX * 0.6)
        tail_n = MAX - head_n
        body = (
            f"{body[:head_n]}\n\n"
            f"... (truncated {len(content) - MAX} chars from the middle; "
            f"original {len(content)} chars) ...\n\n"
            f"{body[-tail_n:]}"
        )
    if source_type == "dockerfile":
        header = "The user submitted the following Dockerfile:"
    elif source_type == "repo":
        header = "The user submitted a git repo. Below is a concatenation of manifest + source files:"
    else:
        header = f"The user submitted the following {interpreter} script:"

    # Per-step Q&A propagation (issue #18). On follow-up turns include
    # any prior user answers verbatim so the LLM doesn't re-ask.
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
                "\n\nPrevious user answers (incorporate into your analysis — "
                "do NOT re-ask the same questions):\n"
                + "\n".join(rendered) + "\n"
            )

    return f"{header}\n\n```\n{body}\n```{answers_section}\nAnalyse and return the JSON."


def _coerce_credits(value: Any) -> Optional[int]:
    """Normalise one LLM credit suggestion to a non-negative int. Mirror
    of `node_builder/analyze_prompt._coerce_credits` in the API."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _tier_credits_from_raw(r: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """Mirror of `node_builder/analyze_prompt._tier_credits_from_raw`."""
    out = {
        "suggested_credits_small": _coerce_credits(r.get("suggested_credits_small")),
        "suggested_credits_medium": _coerce_credits(r.get("suggested_credits_medium")),
        "suggested_credits_large": _coerce_credits(r.get("suggested_credits_large")),
    }
    if out["suggested_credits_medium"] is None:
        for key in ("suggested_credits", "cost_in_credits"):
            legacy = _coerce_credits(r.get(key))
            if legacy is not None:
                out["suggested_credits_medium"] = legacy
                break
    return out


def coerce_and_validate(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Post-process LLM JSON into a safe, schema-compliant dict. Mirror
    of `node_builder/analyze_prompt.coerce_and_validate` in the API."""
    r = raw if isinstance(raw, dict) else {}

    def _enum(v, valid, default):
        return v if isinstance(v, str) and v in valid else default

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
    ][:20]

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
    cleaned_egress = cleaned_egress[:20]

    # Per-step Q&A scaffolding (issue #18). Same coerce logic as the
    # API-side mirror — defaults to empty list / null when the LLM
    # omits them.
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
        if not qid or not question or qid in seen_ids:
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
    cleaned_questions = cleaned_questions[:3]

    raw_glue = r.get("glue_artifact")
    cleaned_glue = None
    if isinstance(raw_glue, dict):
        artifact_type = str(raw_glue.get("artifact_type") or "").strip()
        content = str(raw_glue.get("content") or "")
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
        "input_type": _enum(r.get("input_type"), VALID_INPUT_TYPES, "other"),
        "output_type": _enum(r.get("output_type"), VALID_OUTPUT_TYPES, "other"),
        "process_type": _enum(raw_proc, VALID_PROCESS_TYPES, "CPU"),
        "suggested_name": str(r.get("suggested_name") or "")[:80],
        "suggested_description": str(r.get("suggested_description") or "")[:400],
        "env_vars": env_vars,
        "allowed_egress": cleaned_egress,
        "confidence": _enum(r.get("confidence"), VALID_CONFIDENCE, "low"),
        "schema_confidence": _enum(
            r.get("schema_confidence"), VALID_CONFIDENCE,
            # Falls back to the overall confidence rather than to "low": the
            # API copy treats a low schema_confidence as a signal to re-ask the
            # user, and defaulting to low would make every response from an LLM
            # that omits the field look uncertain about its schema.
            _enum(r.get("confidence"), VALID_CONFIDENCE, "low")),
        "reasoning": str(r.get("reasoning") or "")[:500],
        "clarifying_questions": cleaned_questions,
        "glue_artifact": cleaned_glue,
        **tier_credits,
    }


def fallback_response(reason: str = "") -> Dict[str, Any]:
    return {
        "input_type": "other",
        "output_type": "other",
        "process_type": "CPU",
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


async def analyze_node_tool(
    source_summary: str,
    source_tree: str,
    source_content: str,
    project_name: Optional[str] = None,
    *,
    source_type: str = "repo",
    interpreter: str = "python3",
    user_answers: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run the analysis prompt against the configured LLM provider and
    return a coerced, validated dict. Never raises — on any failure
    returns the fallback_response so the containerizer can still write
    a valid analysis.json sidecar.

    `user_answers` (issue #18) carries any prior turn's answers so the
    LLM converges instead of re-asking. Empty / None on the first
    turn — the LLM either returns a final analysis or asks new
    clarifying_questions.
    """
    provider = get_provider()
    prompt = _user_prompt(source_type, source_content, interpreter, user_answers)
    try:
        raw_text = await provider.generate(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=prompt,
            response_format="json",
        )
        parsed = json.loads(raw_text) if isinstance(raw_text, str) else raw_text
    except Exception as exc:
        return fallback_response(f"Analysis unavailable: {exc}")

    return coerce_and_validate(parsed if isinstance(parsed, dict) else {})
