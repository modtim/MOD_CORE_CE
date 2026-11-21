"""Stage 7 — generate the four MOD-node artifacts in parallel.

Takes the Stage 6 build profile + the Qdrant collection from Stage 4,
fires off four concurrent LLM calls (one per output file), and
assembles the result into the same dict shape the current wizard
expects (so the API's `_extract_albedo_artifacts` path keeps working).

Every generator's output is raw text — not JSON-wrapped — which
eliminates the nested-JSON parser failure mode we patched in commit
9c2ac1e8.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

try:
    from tools import embed_index
    _RAG_AVAILABLE = True
except ImportError:   # pragma: no cover
    _RAG_AVAILABLE = False

from tools import artifact_prompts
from tools.llm_providers import LLMConfig, get_provider


logger = logging.getLogger(__name__)


ARTIFACT_TIMEOUT_SECONDS = 240
MAX_RAG_CHUNKS = 6


@dataclass
class ArtifactResult:
    dockerfile: str = ""
    entrypoint_sh: str = ""
    build_and_push_sh: str = ""
    node_metadata_py: str = ""
    image_name: str = ""
    # Debug / telemetry — kept separate from the raw files so the
    # UI's preview doesn't render them.
    errors: dict[str, str] = None
    # The build profile dict actually used for generation, returned
    # verbatim so the UI can show the user what decisions were made.
    build_profile: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "success": not (self.errors or {}),
            "dockerfile": self.dockerfile,
            "entrypoint_sh": self.entrypoint_sh,
            "build_and_push_sh": self.build_and_push_sh,
            "node_metadata_py": self.node_metadata_py,
            "image_name": self.image_name,
            "errors": self.errors or {},
            "build_profile": self.build_profile,
        }


def _join_image_ref(prefix, *parts):
    """Same rule as CORE/API/utils/registries.py join_image_ref.

    The Containerizer runs standalone in its own container (its Dockerfile
    COPYs scripts/ only) and cannot import the API util, so the rule is
    implemented inline here: an empty/None/whitespace-only/slash-only
    registry yields a BARE ref (the CE shape — the image lives on the
    building machine's own daemon); a set registry yields
    `prefix/part`, byte-identical to the old f-string. A leading '/' is
    never produced: it is not a valid Docker image reference.
    """
    if prefix is None:
        prefix = ""
    prefix = prefix.strip().rstrip("/")
    segs = [p.lstrip("/") for p in parts if p]
    if prefix:
        return f"{prefix}/{'/'.join(segs)}"
    return "/".join(segs)


async def generate(
    build_profile: dict,
    llm: LLMConfig,
    *,
    collection: Optional[str] = None,
    registry: str = "example.com",
) -> ArtifactResult:
    """Launch all 4 artifact LLM calls concurrently, collect results."""
    provider = get_provider(llm)
    node_name = (build_profile.get("node_name") or "mod_node").strip() or "mod_node"
    image_name = _join_image_ref(registry, node_name)

    # Build per-artifact user prompts. Each gets its own RAG retrieval
    # query so the Dockerfile sees import/apt code, entrypoint sees
    # CLI code, etc. build-and-push needs nothing from RAG — it's a
    # template filled from the build profile.
    dockerfile_user = _render_dockerfile_prompt(build_profile, collection)
    entrypoint_user = _render_entrypoint_prompt(build_profile, collection)
    build_push_user = _render_build_push_prompt(build_profile, image_name)
    dag_user = _render_metadata_prompt(build_profile, collection)

    async def _call(system: str, user: str, label: str) -> tuple[str, str]:
        try:
            text = await asyncio.wait_for(
                provider.generate(system, user),
                timeout=ARTIFACT_TIMEOUT_SECONDS,
            )
            return label, _strip_fences(text)
        except asyncio.TimeoutError:
            raise RuntimeError(f"{label} timeout after {ARTIFACT_TIMEOUT_SECONDS}s")

    tasks = [
        _call(artifact_prompts.DOCKERFILE_PROMPT, dockerfile_user, "dockerfile"),
        _call(artifact_prompts.ENTRYPOINT_PROMPT, entrypoint_user, "entrypoint_sh"),
        _call(artifact_prompts.BUILD_AND_PUSH_PROMPT, build_push_user, "build_and_push_sh"),
        _call(artifact_prompts.NODE_METADATA_PROMPT, dag_user, "node_metadata_py"),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    out = ArtifactResult(image_name=image_name, errors={}, build_profile=build_profile)
    for r in results:
        if isinstance(r, Exception):
            # Grab label from the exception message; fall back to "?".
            msg = str(r)
            for label in ("dockerfile", "entrypoint_sh", "build_and_push_sh", "node_metadata_py"):
                if msg.startswith(label):
                    out.errors[label] = msg; break
            else:
                out.errors["unknown"] = msg
            continue
        label, text = r
        setattr(out, label, text)
    return out


# ---------------------------------------------------------------------------
# Per-artifact user-prompt builders
# ---------------------------------------------------------------------------

def _render_dockerfile_prompt(profile: dict, collection: Optional[str]) -> str:
    parts = [_profile_summary(profile),
             _profile_deps_section(profile),
             _profile_env_section(profile),
             _profile_egress_section(profile),
             _profile_entry_section(profile),
             _profile_gpu_section(profile)]
    parts.append(_rag_section(
        collection,
        query=_rag_query_from_profile(profile, extras=[
            "import", "CUDA", "torch.cuda", "apt-get",
            "requirements.txt", "pip install",
        ]),
        language_filter=None,
        header="Relevant code excerpts (imports / CUDA / env):",
    ))
    return "\n\n".join(p for p in parts if p)


def _render_entrypoint_prompt(profile: dict, collection: Optional[str]) -> str:
    parts = [_profile_summary(profile),
             _profile_entry_section(profile),
             _profile_env_section(profile),
             _profile_io_section(profile)]
    parts.append(_rag_section(
        collection,
        query=_rag_query_from_profile(profile, extras=[
            "argparse", "click", "def main", "__main__", "sys.argv",
            "/data/input/", "/data/output/",
        ]),
        language_filter=None,
        header="Relevant code excerpts (CLI / main / I/O paths):",
    ))
    return "\n\n".join(p for p in parts if p)


def _render_build_push_prompt(profile: dict, image_name: str) -> str:
    return (
        f"Node name: {profile.get('node_name')}\n"
        f"Image name: {image_name}\n"
        f"Standard MOD registry: {image_name.split('/', 1)[0]}\n"
        f"Description: {profile.get('description', '')}\n"
    )


def _render_metadata_prompt(profile: dict, collection: Optional[str]) -> str:
    parts = [_profile_summary(profile),
             _profile_io_section(profile),
             _profile_env_section(profile),
             _profile_egress_section(profile),
             _profile_gpu_section(profile)]
    parts.append(_rag_section(
        collection,
        query=_rag_query_from_profile(profile, extras=[
            "os.environ", "os.getenv", "/data/input/", "/data/output/",
            "reads_from", "writes_to",
        ]),
        language_filter=None,
        header="Relevant code excerpts (env / I/O):",
    ))
    return "\n\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Profile pretty-printers — consistent compact summaries each prompt reuses
# ---------------------------------------------------------------------------

def _profile_summary(p: dict) -> str:
    return (
        f"=== Build profile summary ===\n"
        f"Node name: {p.get('node_name')}\n"
        f"Description: {p.get('description', '')}\n"
        f"Runtime: {(p.get('runtime') or {}).get('language')} "
        f"{(p.get('runtime') or {}).get('version', '')}\n"
        f"Base image hint: {p.get('base_image_hint')}\n"
        f"GPU required: {p.get('gpu_required')}\n"
        f"Reasoning: {p.get('reasoning', '')}\n"
    )


def _profile_deps_section(p: dict) -> str:
    deps = p.get("dependencies") or {}
    if not deps:
        return ""
    lines = ["=== Dependencies ==="]
    for ecosystem, items in deps.items():
        if not items:
            continue
        lines.append(f"{ecosystem}: " + ", ".join(items))
    return "\n".join(lines)


def _profile_env_section(p: dict) -> str:
    envs = p.get("env_vars") or []
    if not envs:
        return ""
    lines = ["=== Env vars ==="]
    for e in envs:
        if not isinstance(e, dict):
            continue
        lines.append(
            f"- {e.get('name')} (required={e.get('required', False)}, "
            f"default={e.get('default')}): {e.get('purpose', '')}"
        )
    return "\n".join(lines)


def _profile_egress_section(p: dict) -> str:
    eg = p.get("egress_allowlist") or []
    if not eg:
        return ""
    return "=== Egress allowlist ===\n" + "\n".join(f"- {h}" for h in eg)


def _profile_entry_section(p: dict) -> str:
    ep = p.get("entry_point") or {}
    if not ep:
        return ""
    return (
        f"=== Entry point ===\n"
        f"Kind: {ep.get('kind')}\n"
        f"Path or command: {ep.get('path_or_cmd')}\n"
        f"Why chosen: {ep.get('explanation', '')}\n"
    )


def _profile_io_section(p: dict) -> str:
    io = p.get("io_shape") or {}
    if not io:
        return ""
    return (
        f"=== I/O shape ===\n"
        f"Input type: {io.get('input_type')}\n"
        f"Output type: {io.get('output_type')}\n"
        f"Reads from: {io.get('reads_from', '/data/input/')}\n"
        f"Writes to: {io.get('writes_to', '/data/output/')}\n"
    )


def _profile_gpu_section(p: dict) -> str:
    rh = p.get("resource_hints") or {}
    if not rh:
        return ""
    return (
        f"=== Resource hints ===\n"
        f"CPU cores: {rh.get('cpu_cores', 'unspecified')}\n"
        f"Memory GB: {rh.get('memory_gb', 'unspecified')}\n"
        f"GPU type: {rh.get('gpu_type', 'cpu_only')}\n"
        f"Disk GB: {rh.get('disk_gb', 'unspecified')}\n"
    )


# ---------------------------------------------------------------------------
# RAG plumbing
# ---------------------------------------------------------------------------

def _rag_query_from_profile(profile: dict, *, extras: list[str]) -> str:
    bits: list[str] = [profile.get("node_name") or "", profile.get("description") or ""]
    for ecosystem, deps in (profile.get("dependencies") or {}).items():
        bits.append(ecosystem)
        bits.extend(list(deps or [])[:20])
    bits.extend(extras)
    return " ".join(str(b) for b in bits if b)


def _rag_section(
    collection: Optional[str], query: str, language_filter: Optional[str],
    header: str,
) -> str:
    if not collection or not _RAG_AVAILABLE:
        return ""
    try:
        chunks = embed_index.search(
            collection, query, top_k=MAX_RAG_CHUNKS,
            language_filter=language_filter,
        )
    except Exception as e:
        logger.debug("RAG retrieve failed: %s", e)
        return ""
    if not chunks:
        return ""
    out = [header]
    for i, c in enumerate(chunks, 1):
        snippet = (c.get("text") or "").strip()
        if not snippet:
            continue
        loc = f"{c.get('path')}:{c.get('start_line')}-{c.get('end_line')}"
        # Trim each chunk so the combined block fits
        if len(snippet) > 1200:
            snippet = snippet[:1200] + "\n...[snipped]"
        out.append(f"\n--- excerpt {i} ({loc}) ---\n{snippet}")
    return "\n".join(out)


_FENCE_RE = re.compile(r"^```(?:[a-zA-Z0-9]+)?\n?(.*?)\n?```\s*$", re.DOTALL)


def _strip_fences(text: str) -> str:
    """Sometimes the LLM wraps output in a ``` fence despite instructions.
    Unwrap cleanly."""
    text = text.strip()
    m = _FENCE_RE.match(text)
    if m:
        return m.group(1).strip()
    return text
