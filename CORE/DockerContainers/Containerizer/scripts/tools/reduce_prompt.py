"""System prompt + JSON schema for the reduce step.

Stage 6 of the codebase pipeline. Runs once per codebase, AFTER all
module analyses are in. Inputs: every module summary from Stage 5 +
parsed manifests + existing Dockerfiles (if any). Output: a single
"build profile" — the structured plan that Stage 7's four artifact
generators all read from.

Key constraint reinforced in the prompt: the output of this pipeline
is ONE MOD node (four files). If the codebase looks like multiple
services, the reduce step picks the most prominent entry point and
declares that as "the node" rather than inventing a multi-node topology.
"""

SYSTEM_PROMPT = """\
You synthesise a set of per-module analyses into a single build plan.
The ultimate output of this pipeline is exactly one MOD node, which is
exactly four files:

  - Dockerfile
  - entrypoint.sh
  - build-and-push.sh
  - node metadata .py

Your job is to produce a build profile JSON that the four downstream
generators read from. You are not generating any of those files — you
are deciding what they should contain.

Return a single JSON object, no prose, no code fences, no extra keys:

{
  "node_name": "<snake_case, <=40 chars, derive from the dominant module's purpose>",
  "description": "<one sentence describing what the node does when it runs>",

  "runtime": {
    "language": "<Python | Node | Go | Ruby | Java | Rust | ...>",
    "version": "<e.g. 3.11 | 20 | 1.22 | ...>"
  },

  "base_image_hint": "<variant name from the curated MOD catalog: python-3.11, cuda-12.4-python3.11, node-20, etc. Prefer CUDA variants only when gpu_required=true.>",

  "gpu_required": <bool — true iff any module marked gpu_required OR the dep list includes torch/tensorflow/jax/cupy>,

  "entry_point": {
    "kind": "<script | module | command>",
    "path_or_cmd": "<e.g. app/main.py | python -m my_pkg | ./run.sh>",
    "explanation": "<one sentence saying why you picked this one from among the candidates>"
  },

  "dependencies": {
    "python": ["torch==2.4.0", "transformers>=4.40", ...],
    "apt": ["ffmpeg", "libgl1", ...],
    "npm": [...],
    "other": [...]
  },

  "env_vars": [
    {"name": "HF_TOKEN", "required": false, "purpose": "optional hf auth for gated models", "default": null}
  ],

  "egress_allowlist": ["huggingface.co", "cdn-lfs.huggingface.co", ...],

  "io_shape": {
    "input_type": "<image | text | audio | video | tabular | json | binary | none | other>",
    "output_type": "<image | text | audio | video | tabular | json | binary | none | other>",
    "reads_from": "/data/input/",
    "writes_to": "/data/output/"
  },

  "resource_hints": {
    "cpu_cores": <int>,
    "memory_gb": <int>,
    "gpu_type": "<nvidia | amd | intel_xpu | cpu_only>",
    "disk_gb": <int estimate of image + model cache>
  },

  "reasoning": "<2-4 sentences explaining why you made the key choices above — useful for users reviewing the preview>",

  "open_questions": [
    "<question the user should answer before merging this node — empty list is fine>"
  ],

  "confidence": "<high | medium | low>"
}

Rules:

1. ONE node. If the input describes what looks like 3 services, pick
   the most prominent entry point and write its build plan. Surface
   the alternatives in `open_questions` so the user can choose later.
2. Never invent a `docker-compose.yml`, per-service Dockerfile, or
   multi-image plan. Four files is the hard contract.
3. Aggregate dependencies: every manifest dep goes into
   `dependencies.python` / `.npm` / etc., keyed by ecosystem. If a
   dep is dev-only, drop it.
4. Merge egress: every egress host from every module's analysis goes
   into `egress_allowlist`. Deduplicate. Sort alphabetically.
5. Merge env vars: take the union, prefer `required: true` when any
   module says so.
6. `base_image_hint` must be a curated MOD base image name; don't
   invent one. The Containerizer will fall back to `python-3.11` if
   your hint isn't in the catalog.
7. If gpu_required=true you MUST pick a CUDA/ROCm base image. If the
   runtime isn't Python the available CUDA bases may not apply — set
   `confidence: "medium"` and note in `reasoning`.
8. `resource_hints.memory_gb` should be a realistic estimate (4 for
   small web apps, 16+ for ML inference, 32+ for LLM inference).
9. `open_questions` is the escape hatch — when the codebase is
   ambiguous (multiple plausible entry points, unclear purpose), use
   it. Better to ask than to guess wrong.
"""
