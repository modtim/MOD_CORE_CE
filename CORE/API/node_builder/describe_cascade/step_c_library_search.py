"""wizard-rebuild §8 step C — library search.

LLM names PyPI / npm libraries that would satisfy the intent; each
named library is verified to exist via the registry's metadata
endpoint. Picked library flows into Script mode with a thin
wrapper that imports it.

Rate-limited per backend (PyPI + npm separately). Cache hits
don't consume window — the PyPI/npm metadata lookups are
deterministic so a cache layer is plausible (not implemented in
v1; the rate-limit infrastructure is ready for it).
"""
from __future__ import annotations

import json
import logging
from typing import List

import requests

from ._types import Candidate, StepResult
from . import rate_limits
from utils.registries import platform_image

logger = logging.getLogger(__name__)


def _ask_llm_for_libs(description: str) -> List[dict]:
    """Ask the LLM for ≤5 library suggestions per ecosystem with a
    short rationale + confidence. Returns
    `[{ecosystem, name, rationale, confidence}, ...]`."""
    try:
        from utils.llm_client import call_wizard_llm
        prompt = (
            f"User wants to build a containerized tool that does: {description!r}\n\n"
            "Suggest up to 5 PyPI libraries AND up to 5 npm libraries "
            "that, when imported in a small wrapper script, would "
            "satisfy the user's intent. For each library include a "
            "short rationale + confidence (0..1) in how well it fits.\n\n"
            "Return JSON only: "
            '{"pypi": [{"name": "<pkg>", "rationale": "<reason>", "confidence": <float>}], '
            '"npm":  [{"name": "<pkg>", "rationale": "<reason>", "confidence": <float>}]}'
        )
        resp = call_wizard_llm(
            system_prompt="You are a library suggester. Output only JSON.",
            user_prompt=prompt,
            response_format="json",
            timeout_s=30,
            _label="cascade-rank",  # was purpose= (unsupported kwarg → ranker silently disabled)
        )
        out: List[dict] = []
        for ecosystem in ("pypi", "npm"):
            for it in (resp.get(ecosystem) or [])[:5]:
                if not isinstance(it, dict):
                    continue
                name = (it.get("name") or "").strip()
                if not name:
                    continue
                conf = float(it.get("confidence") or 0.0)
                out.append({
                    "ecosystem": ecosystem,
                    "name": name,
                    "rationale": (it.get("rationale") or "")[:300],
                    "confidence": min(1.0, max(0.0, conf)),
                })
        return out
    except Exception as exc:
        logger.warning("step_c LLM library suggestion failed: %s", exc)
        return []


def _verify_pypi(name: str) -> bool:
    """True iff the PyPI metadata endpoint returns 200 for `name`."""
    try:
        r = requests.get(
            f"https://pypi.org/pypi/{name}/json", timeout=8,
        )
        return r.ok
    except Exception:
        return False


def _verify_npm(name: str) -> bool:
    """True iff the npm registry returns 200 for `name`."""
    try:
        r = requests.get(
            f"https://registry.npmjs.org/{name}", timeout=8,
        )
        return r.ok
    except Exception:
        return False


def _make_wrapper_script(library: dict) -> str:
    """Return a minimum Script-mode wrapper that imports the library.
    User can edit later via the wizard's Edit Node UI."""
    if library["ecosystem"] == "pypi":
        return (
            "#!/usr/bin/env python3\n"
            "import pathlib\n"
            f"import {library['name'].replace('-', '_')}  # noqa: F401\n"
            "\n"
            "in_dir = pathlib.Path('/data/input')\n"
            "out_dir = pathlib.Path('/data/output'); out_dir.mkdir(exist_ok=True)\n"
            "(out_dir / 'result.txt').write_text(\n"
            f"    'TODO: implement {library['name']} call on /data/input → /data/output\\n'\n"
            ")\n"
        )
    # npm wrapper — node script
    return (
        "// Wizard-generated stub. Edit to call the actual API.\n"
        "const fs = require('fs');\n"
        "const path = require('path');\n"
        f"const lib = require({library['name']!r});\n"
        "const outDir = '/data/output';\n"
        "fs.mkdirSync(outDir, {recursive: true});\n"
        f"fs.writeFileSync(path.join(outDir, 'result.txt'), 'TODO: implement {library['name']} call on /data/input → /data/output\\n');\n"
    )


def run_step_c(description: str, tenant_id: int) -> StepResult:
    """Library search. Two backends (PyPI + npm) with independent
    rate-limit windows."""
    result = StepResult(step="C")
    libs = _ask_llm_for_libs(description)
    if not libs:
        return result
    # Group by ecosystem so we batch the per-backend rate-limit
    # check + don't double-spend a PyPI window on multiple lookups
    # in one call (each lookup IS a backend hit per the design).
    for backend in ("pypi", "npm"):
        for lib in [l for l in libs if l["ecosystem"] == backend]:
            rate = rate_limits.check_and_inc(backend, tenant_id)
            if not rate["allowed"]:
                result.rate_limited[backend] = rate
                continue
            verifier = _verify_pypi if backend == "pypi" else _verify_npm
            if not verifier(lib["name"]):
                # Library doesn't exist — refund the increment.
                rate_limits.record_cache_hit(backend, tenant_id)
                continue
            ext = "py" if backend == "pypi" else "js"
            result.candidates.append(Candidate(
                kind="library",
                label=f"{lib['name']} ({backend})",
                submission={
                    "inline_source_body": _make_wrapper_script(lib),
                    "inline_source_filename": f"main.{ext}",
                    "wrap_base_image": (
                        platform_image("base-python:3.11")
                        if backend == "pypi" else "node:20-alpine"
                    ),
                },
                confidence=lib["confidence"],
                backend=backend,
                eta_seconds=300,
                reason=lib["rationale"],
            ))
    result.candidates.sort(key=lambda c: -c.confidence)
    result.candidates = result.candidates[:10]
    return result
