"""δ wizard auto-repair loop — failure classification + LLM repair prompts.

When the test-and-repair build-task stage runs a wizard-built image
against synthetic input and gets back "exit 0 but no output produced"
(or any other failure), the operator's old experience was a hard fail
with a message saying "fix your entrypoint." The new experience: this
module's helpers classify the failure, ship a corrections prompt to
the on-prem wizard LLM, and the loop tries again.

Two repair modes:
  - **inner**: rewrite the entrypoint scripts only. Docker layer cache
    catches everything before `COPY scripts/`, so the rebuild is fast.
    Right for logic bugs (wrong filename, no output written, off-by-one
    in arg parsing).
  - **outer**: rewrite the Dockerfile too. Slow rebuild. Right when the
    failure indicates a missing dependency (ModuleNotFoundError) or a
    missing CLI tool (`/bin/sh: ffmpeg: not found`), because a script
    fix alone can't add a package the image doesn't ship with.

Failure classification: cheap regex/substring matches against stderr.
We err on the side of staying in the inner loop — outer rebuilds cost
real wall-clock time even with the LLM essentially free on-prem.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional


# Substring → "outer" indicators. If any of these are in stderr, the
# script can't fix it alone; we need a Dockerfile change.
_OUTER_TRIGGERS = (
    "ModuleNotFoundError",
    "ImportError: No module named",
    "executable file not found",
    "not found in $PATH",
    "command not found",
    "No such file or directory: '/usr/local/bin/",
    "/bin/sh: 1: ",   # busybox-style missing-cmd
    "/bin/bash: line ",
)

# These are inner-fixable even though they look like missing-cmd messages.
_OUTER_TRIGGER_EXEMPTIONS = (
    # "command not found: foo" where foo is a USER script name — that's
    # the script itself missing because COPY missed a file, not a system
    # dep. The inner loop can fix it by re-emitting the missing file.
    "command not found: /app/scripts/",
    "command not found: /data/",
)


# "Entrypoint never invoked" failure class. The curated MOD base images set
# ENTRYPOINT ["/usr/bin/tini", "--"] with NO CMD; a generated Dockerfile that
# sets neither its own ENTRYPOINT nor a CMD inherits a bare `tini --` that has
# nothing to exec — at runtime tini prints its usage message and exits. The
# fix is ALWAYS a Dockerfile change (add ENTRYPOINT / COPY the entrypoint
# script), never a script edit — so this MUST route to the outer loop.
# Without this, the classifier saw a generic non-zero exit, stayed inner
# (script-only edits), and thrashed forever on phantom "no input files"
# theories (live "Multiply Numbers" build: 7 wasted attempts).
_MISSING_ENTRYPOINT_PATTERNS = (
    re.compile(r"usage:\s*tini\b", re.IGNORECASE),
    re.compile(r"\btini\s+version\b", re.IGNORECASE),
    re.compile(r"\btini\b[^\n]*\bzombie[- ]reap", re.IGNORECASE),
    # tini WITH a program that isn't there → base entrypoint present but the
    # child's entrypoint script was never COPY'd. Same fix family (Dockerfile).
    re.compile(r"\btini\b[^\n]*exec[^\n]*no such file", re.IGNORECASE),
)


def is_missing_entrypoint_failure(failure: "FailureContext") -> bool:
    """True when the failure is the "entrypoint never invoked / bare tini"
    class (see ``_MISSING_ENTRYPOINT_PATTERNS``). Fixable only by a Dockerfile
    change, so callers route it to the outer loop with explicit ENTRYPOINT
    guidance instead of letting the inner loop edit scripts forever."""
    text = (failure.stderr_tail or "") + "\n" + (failure.stdout_tail or "")
    return any(p.search(text) for p in _MISSING_ENTRYPOINT_PATTERNS)


@dataclass(frozen=True)
class FailureContext:
    """What the test-and-repair stage observed after one execution."""
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    produced_output: bool
    output_files: List[Dict[str, int]]  # [{name, size}, ...]
    input_seed: Dict[str, str]          # {source, filename, input_type}
    # --help grounding: real command surface probed from the built image
    # when the failure was a missing/hallucinated command. Ground truth
    # (which binaries exist + their --help + what's actually installed)
    # so the repair corrects against reality, not the model's guess.
    binary_reality: Optional[str] = None


def classify_failure_mode(failure: FailureContext) -> str:
    """Return "inner" or "outer" — which repair loop should handle this.

    Default is "inner". We escalate to "outer" only when stderr shows a
    signal that no amount of script editing can fix.
    """
    if failure.exit_code == 0 and failure.produced_output:
        return "none"  # shouldn't be called, but defensive
    stderr = failure.stderr_tail or ""
    # Entrypoint-never-invoked (bare tini) is ALWAYS a Dockerfile fix — check
    # it before the inner-favouring exemptions so it can't be mis-routed to a
    # script-only inner repair (which can never add an ENTRYPOINT).
    if is_missing_entrypoint_failure(failure):
        return "outer"
    # Exemptions first — a "command not found" on a USER script path
    # stays inner-fixable.
    for ex in _OUTER_TRIGGER_EXEMPTIONS:
        if ex in stderr:
            return "inner"
    for tr in _OUTER_TRIGGERS:
        if tr in stderr:
            return "outer"
    return "inner"


def short_failure_reason(failure: FailureContext) -> str:
    """One-line human-readable failure reason for logs + UI."""
    if is_missing_entrypoint_failure(failure):
        return (
            "container has no ENTRYPOINT — base image's tini ran with no "
            "program to exec (Dockerfile is missing ENTRYPOINT/CMD)"
        )
    if failure.exit_code != 0:
        return f"container exited {failure.exit_code}"
    if not failure.produced_output:
        return "container exited 0 but /data/output was empty"
    return "unknown failure"


# ---------------------------------------------------------------------
# LLM repair prompts
# ---------------------------------------------------------------------


_INNER_SYSTEM_PROMPT = """You are a repair assistant for the MOD wizard \
node builder. A user has built a container that's supposed to read \
input from /data/input and write output to /data/output. The container \
just failed a smoke test. You will be given the failure details and \
the CURRENT entrypoint scripts. Your job is to return UPDATED scripts \
that fix the failure.

Constraints you MUST follow:
1. The container's entrypoint already runs unchanged — only the scripts \
   it calls can be rewritten. Do NOT change the Dockerfile or add new \
   files; only the existing scripts may be edited.
2. The fix must make the container read from /data/input/<file> and \
   write at least one non-empty file under /data/output/.
3. Preserve any working logic — only change what the failure indicates \
   is broken.
4. Return JSON only, matching the schema below. No prose, no \
   explanations outside the explanation field.

Response schema:
{
  "explanation": "<one short sentence on what you changed and why>",
  "updated_scripts": {
    "<filename>": "<new file contents>",
    ...
  }
}

Only include files you actually changed. Files you don't include keep \
their previous contents."""


_IMAGE_MODE_OUTER_SYSTEM_PROMPT = """You are a repair assistant for the \
MOD wizard node builder running in IMAGE MODE.

The user picked a specific Docker image (the `FROM` line). The wizard \
built a thin adapter on top: `FROM <user-image>` + a tiny ENTRYPOINT \
shim that bridges /data/input → the image → /data/output. The smoke \
test failed.

CRITICAL — DO NOT CHANGE THE FROM LINE:
The user explicitly chose this base image. If you swap it for any \
other image (curated base, CUDA base, etc.) you've broken the user's \
intent. ALL prior outer-repair attempts that changed FROM in this run \
have been wrong. The FROM line is FIXED — preserve it byte-for-byte \
in your response.

What you ARE allowed to change:
- RUN directives BELOW the FROM (install missing packages, set env)
- The COPY and ENTRYPOINT directives (rare, but allowed)
- entrypoint.sh contents (most common fix point — adapt how the \
  container's native tool is invoked)

What the failure usually means in Image mode:
- "command not found" → add a `RUN apt-get install` or `RUN pip install`
- The entrypoint.sh doesn't know HOW to invoke the user's image →
  rewrite entrypoint.sh to match the image's actual CLI / Python API
- The image expects input via stdin/argv but the shim writes files →
  bridge in entrypoint.sh

CRITICAL — DO NOT add Docker-related anything (same as standard repair):
The container will NEVER need Docker at runtime. NEVER add \
`docker.io`, `docker-ce`, `groupadd docker`, dind, etc.

NOTE on `USER mod` errors:
The Image-mode adapter is intentionally USER-less — the base image's \
runtime user prevails. If you see "unable to find user mod" / "no \
matching entries in passwd file", the wizard's standard adapter (not \
Image mode) probably emitted a `USER mod` directive against a public \
base that has no `mod` user. The fix is the idempotent useradd block \
documented in the outer prompt, NOT removing `USER mod` (security \
lint rule requires non-root final USER). For pure Image-mode plans, \
the renderer should not emit `USER mod` at all when the base has no \
prior USER directive.

Constraints:
1. Preserve the original FROM line exactly.
2. The final image must still read /data/input → write /data/output.
3. Pin Python versions that actually exist on PyPI.
4. Return JSON only, schema below.

Response schema:
{
  "explanation": "<one short sentence>",
  "updated_dockerfile": "<full Dockerfile contents — FROM unchanged>",
  "updated_scripts": {"<filename>": "<contents>", ...}
}"""


_OUTER_SYSTEM_PROMPT = """You are a repair assistant for the MOD wizard \
node builder. A previous build's smoke test failed in a way that a \
script-only fix can't address — typically a missing system command or \
Python package. You will be given the failure details, the CURRENT \
Dockerfile, and the CURRENT entrypoint scripts. Return an UPDATED \
Dockerfile (and optionally updated scripts) that fixes the failure.

CRITICAL — "Usage: tini" / bare-tini means a MISSING ENTRYPOINT:
The curated MOD base images declare `ENTRYPOINT ["/usr/bin/tini", "--"]` \
with NO CMD. If the container's output shows tini's usage message \
("Usage: tini ...", "tini version ...") or tini reports it can't exec a \
program, the Dockerfile is MISSING an ENTRYPOINT (or a CMD). The base's \
tini has nothing to run. This is NEVER a "no input files" or script-logic \
problem. Fix it by ensuring the Dockerfile BOTH copies the entrypoint \
script in AND ends with an explicit entrypoint, e.g.:
  COPY --chmod=0755 entrypoint.sh /app/entrypoint.sh
  ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
Do NOT edit the scripts to chase phantom input errors — add the ENTRYPOINT.

CRITICAL — DO NOT add Docker-related anything:
The container will NEVER need to run Docker commands at runtime. The \
build environment is also already inside Docker. If you see error text \
mentioning "docker", "daemon", or "socket", that is almost always \
coming from the OUTER docker daemon during the build, NOT from inside \
your container. NEVER add any of these to the Dockerfile:
  • `apt-get install docker.io / docker-ce / docker-ce-cli`
  • `groupadd docker` or `useradd ... -g docker`
  • `touch /var/run/docker.sock` or any /var/run/docker.sock setup
  • `docker-in-docker`, `dind`, `tini` for docker init
  • `pip install docker / docker-py / docker-compose`
If a previous repair attempt added any of these, REMOVE them in your \
response — they are corrupting the Dockerfile, not fixing it.

CRITICAL — `USER mod` requires the user to exist:
Curated mod_core base images (base-cuda, base-python, base-rocm, \
base-intel) provision uid 10001 as `mod`. PUBLIC bases \
(python:3.11-slim, ubuntu, debian, alpine, pytorch/pytorch, etc.) DO \
NOT — and `USER mod` against them trips docker exec with "no \
matching entries in passwd file". When the FROM line is a curated \
mod_core image, plain `USER mod` is fine. When it's a public base \
(or any image you can't verify already provisions mod), KEEP or ADD \
this idempotent useradd block immediately before `USER mod`:

  RUN id -u mod >/dev/null 2>&1 || useradd -m -u 10001 mod 2>/dev/null \
      || adduser -D -u 10001 mod

The `id -u mod` short-circuit makes it a no-op on curated bases. \
`useradd -g docker mod` or `useradd ... -G docker ... mod` is still \
prohibited (corrupting pattern — runtime nodes never need docker \
group). Plain `useradd ... mod` (no docker group) is correct here.

Other constraints:
1. Keep the existing FROM base image unless the failure clearly \
   requires a different one. Adding apt/pip packages to an existing \
   base is strongly preferred over switching bases.
2. The final image must still read /data/input → write /data/output.
3. Pin Python package versions to ones that actually exist on PyPI. \
   Common gotchas: gradio max version is 5.x (NOT 6.x). If you see a \
   `pip install X==Y.Z` line failing with "No matching distribution", \
   either remove the pin or downgrade to a real released version.
4. Return JSON only, schema below. No prose outside `explanation`.

Response schema:
{
  "explanation": "<one short sentence on what you changed and why>",
  "updated_dockerfile": "<full Dockerfile contents>",
  "updated_scripts": {"<filename>": "<contents>", ...}
}

`updated_scripts` is optional — include only files you actually \
changed. `updated_dockerfile` MUST be present and complete."""


# Prompt-size caps are now derived from the LLM's actual context
# window via node_builder.llm_budget.PromptBudgets — see that module
# for the why. Legacy constants kept here ONLY as the safety-net
# defaults that fire when no budgets= is passed (covers test paths
# that haven't been migrated and gives a sensible 64K-context floor).
# In production, default_budgets() reads max_model_len from the live
# LLM endpoint and these constants are never consulted.
_LEGACY_PER_SCRIPT_CHAR_CAP = 5_000
_LEGACY_TOTAL_SCRIPTS_CHAR_CAP = 32_000
_LEGACY_DOCKERFILE_CHAR_CAP = 8_000


def _truncate_with_marker(text: str, limit: int, *, keep: str = "tail") -> str:
    """Trim `text` to `limit` chars with a "[truncated …]" marker so the
    LLM doesn't think the file was actually that short.

    `keep` controls which end survives:
      - "tail" (default): keep the last `limit` chars. Right for build
        logs / stderr where the error message is at the bottom.
      - "head": keep the first `limit` chars. Right for Dockerfiles
        where the FROM line at the top is critical (the base-image rail
        validates it, and the LLM needs to know what base it's amending).
    """
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    if keep == "head":
        return (
            text[:limit] +
            f"\n[truncated — {omitted} more chars omitted after this point]"
        )
    return (
        f"[truncated — {omitted} chars omitted from start; "
        f"showing last {limit}]\n"
        + text[-limit:]
    )


def _budget_scripts(scripts: Dict[str, str], budgets=None) -> Dict[str, str]:
    """Cap each script's content + the total payload so the LLM prompt
    can't blow the context window. Files past the total cap are dropped
    (sorted alphabetically for determinism) with a placeholder entry
    that names the dropped files so the LLM doesn't pretend they don't
    exist.

    `budgets`: a PromptBudgets dataclass (from node_builder.llm_budget).
    When None, falls back to the legacy 64K-context defaults. Always
    pass explicitly in new code so the caps track the actual LLM."""
    per_script_cap = (budgets.per_script_chars if budgets is not None
                      else _LEGACY_PER_SCRIPT_CHAR_CAP)
    total_cap = (budgets.total_scripts_chars if budgets is not None
                 else _LEGACY_TOTAL_SCRIPTS_CHAR_CAP)
    out: Dict[str, str] = {}
    running = 0
    dropped: List[str] = []
    for fname in sorted(scripts.keys()):
        content = scripts[fname] or ""
        truncated = _truncate_with_marker(content, per_script_cap)
        # +len(fname)+8 accounts for the "--- {fname} ---\n```\n...\n```"
        # framing each file adds to the eventual prompt.
        cost = len(truncated) + len(fname) + 8
        if running + cost > total_cap:
            dropped.append(fname)
            continue
        out[fname] = truncated
        running += cost
    if dropped:
        out["__dropped_files_note__"] = (
            f"[{len(dropped)} additional file(s) omitted from this prompt "
            f"to stay under the LLM context budget: {', '.join(dropped[:20])}"
            + (" ..." if len(dropped) > 20 else "")
            + ". They exist in the build context but the failure does not "
            "appear to be in them. If you need to edit one, name it in "
            "your response and the next iteration will include its "
            "contents.]"
        )
    return out


def build_repair_user_prompt(
    *,
    mode: str,
    failure: FailureContext,
    current_dockerfile: Optional[str],
    current_scripts: Dict[str, str],
    attempt_number: int,
    attempt_max: int,
    prior_attempts: List[Dict[str, str]],
    budgets=None,
) -> str:
    """Build the user-side prompt with failure context + current files.

    `prior_attempts` is the LLM-explanation trail from earlier loop
    iterations — including it lets the LLM not repeat a fix that
    already didn't work.

    Size-budget aware: per-file and total-payload caps prevent
    multi-megabyte source bundles (TRELLIS-class repos pasted into
    source_files) from blowing the LLM's context window. For
    build-failure inputs the scripts are skipped entirely — only the
    Dockerfile + build error matter when docker build itself didn't
    finish (the source files weren't reached).

    `budgets`: a PromptBudgets dataclass that derives all caps from
    the live LLM's context window. When None, falls back to legacy
    64K-context constants — callers should always pass explicitly in
    new code so caps scale with model swaps.
    """
    dockerfile_cap = (budgets.dockerfile_chars if budgets is not None
                      else _LEGACY_DOCKERFILE_CHAR_CAP)
    lines: List[str] = []
    lines.append(f"Failure mode: {mode}")
    lines.append(f"Attempt: {attempt_number} of {attempt_max}")
    lines.append("")
    lines.append("Failure details:")
    lines.append(f"  exit_code: {failure.exit_code}")
    lines.append(f"  produced_output: {failure.produced_output}")
    if failure.output_files:
        sizes = ", ".join(f"{f['name']}={f['size']}B" for f in failure.output_files)
        lines.append(f"  output_files (note: empty / zero-byte files count as no output): {sizes}")
    lines.append(f"  input_seed: {failure.input_seed}")
    lines.append("")
    lines.append("Last 2000 chars of stderr:")
    lines.append("```")
    lines.append((failure.stderr_tail or "")[-2000:])
    lines.append("```")
    if is_missing_entrypoint_failure(failure):
        # Name the failure class explicitly so a weak model can't mis-read the
        # tini usage message as a runtime/input problem and thrash on scripts.
        lines.append("")
        lines.append(
            "DIAGNOSIS (deterministic): this is the MISSING-ENTRYPOINT class. "
            "The output above is the base image's `tini` printing its usage "
            "because the Dockerfile set no ENTRYPOINT and no CMD, so tini had "
            "nothing to exec. This is NOT an input-file or script-logic "
            "problem. The ONLY correct fix is a Dockerfile change: ensure the "
            "entrypoint script is copied in and add an explicit ENTRYPOINT — "
            'COPY --chmod=0755 entrypoint.sh /app/entrypoint.sh  then  '
            'ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]. Do not modify the '
            "scripts to chase a phantom input error."
        )
    if failure.binary_reality:
        # --help grounding: ground truth probed from the actual image.
        # This is authoritative — it overrides the model's memory of a
        # tool's CLI. If a command is MISSING here, do NOT re-invoke it;
        # if it EXISTS, use ONLY the flags/sub-commands shown in its real
        # --help. The "EXECUTABLES ACTUALLY INSTALLED" list is the real
        # toolset — prefer a tool from there over one you assume exists.
        lines.append("")
        lines.append(
            "GROUND TRUTH — real command surface of this image (probed live; "
            "authoritative, overrides any assumption about a tool's CLI):"
        )
        lines.append("```")
        lines.append(failure.binary_reality[-4000:])
        lines.append("```")
        lines.append(
            "Rules: a command marked MISSING does not exist — do not call it; "
            "either install it (outer/Dockerfile) or switch to one from the "
            "installed list. For a command marked EXISTS, use ONLY the "
            "sub-commands/flags shown in its real --help above."
        )
    if failure.stdout_tail:
        lines.append("")
        lines.append("Last 2000 chars of stdout:")
        lines.append("```")
        lines.append(failure.stdout_tail[-2000:])
        lines.append("```")
    if prior_attempts:
        lines.append("")
        lines.append("Previous repair attempts that did NOT fix the issue:")
        # Cross-attempt memory (move A): show the LAST 10 attempts (up
        # from 5) and include each attempt's stderr_signature so the
        # next iteration can SEE whether prior fixes changed the error
        # or kept hitting the same wall. Without the stderr signature,
        # the LLM's own narration of past attempts (biased to claim
        # "fixed") is the only signal — and the LLM has no way to
        # notice it's cycling through the same fix family.
        for a in prior_attempts[-10:]:
            attempt_n = a.get("attempt", "?")
            mode_str = a.get("mode", "?")
            expl = (a.get("llm_explanation") or "").strip()
            sig = (a.get("stderr_signature") or "").strip()
            lines.append(f"  - attempt {attempt_n} ({mode_str}): {expl}")
            if sig:
                # Show the tail of the error that resulted from this
                # attempt so the LLM can compare across attempts.
                # 1200 chars (up from 300) so compile / wheel-build
                # tracebacks fit — the actual error is often
                # 200-500 chars deep, with the docker outer wrapper
                # taking the next 100. wfr 51 saw the LLM cycling
                # 14 attempts because the real error was truncated
                # off the 300-char tail.
                sig_excerpt = sig[-1200:].replace("\n", " | ")
                lines.append(f"    → error after this fix: {sig_excerpt}")
        lines.append("")
        lines.append(
            "Do not repeat any of those exact changes. If the same error "
            "signature appears across multiple attempts, the current "
            "approach is wrong — step back and propose a structurally "
            "different fix (different base image, different install "
            "strategy, etc.) rather than another surface-level tweak."
        )

    # Build-failure mode: docker build itself failed, so the source files
    # weren't even copied into the image yet. The fix lives in the
    # Dockerfile (missing apt package, wrong pip pin, etc.). Skip the
    # script bundle entirely — the LLM doesn't need it and including it
    # blows the context for repo-shaped nodes.
    is_build_failure = (
        (failure.input_seed or {}).get("source") == "build_failure"
    )

    if mode == "outer" and current_dockerfile:
        lines.append("")
        lines.append("Current Dockerfile:")
        lines.append("```Dockerfile")
        # keep="head" — the FROM directive at the top is the most
        # important line for an outer repair (the rail validates it,
        # and the LLM needs to know what base it's working with).
        lines.append(
            _truncate_with_marker(current_dockerfile, dockerfile_cap, keep="head")
        )
        lines.append("```")
    if current_scripts and not is_build_failure:
        budgeted = _budget_scripts(current_scripts, budgets=budgets)
        lines.append("")
        lines.append("Current scripts:")
        for fname, content in budgeted.items():
            if fname == "__dropped_files_note__":
                lines.append("")
                lines.append(content)
                continue
            lines.append(f"--- {fname} ---")
            lines.append("```")
            lines.append(content)
            lines.append("```")
    elif is_build_failure:
        lines.append("")
        lines.append(
            "[docker build failed before source files were copied; "
            "the fix is in the Dockerfile above. Source-file contents "
            "omitted from this prompt — they will be included if a "
            "later iteration's failure points at a specific script.]"
        )
    return "\n".join(lines)


def system_prompt_for(mode: str, *, is_image_mode: bool = False,
                       has_build_plan: bool = False) -> str:
    """Pick the system prompt for the repair mode.

    `is_image_mode=True` flips outer-repair to the stricter
    IMAGE_MODE_OUTER prompt that explicitly forbids changing the
    FROM line.

    `has_build_plan=True` (wizard-rebuild §7b) flips outer-repair to
    the plan-delta-only schema. Specs with a BuildPlan get a
    structural prompt that asks for 1-3 field changes; the dual-mode
    prompt confused the LLM into emitting partial Dockerfiles
    without FROM directives (manual-test repro 2026-05-25 spec=440
    Test E: two thrash-rejected "no FROM" responses).
    """
    if mode == "outer":
        if is_image_mode:
            return _IMAGE_MODE_OUTER_SYSTEM_PROMPT
        # Keep `has_build_plan` plumbed through but use the legacy
        # full-Dockerfile prompt — the strict plan-delta-only prompt
        # had a lower LLM compliance rate (returned partial dockerfiles
        # missing FROM directives, manual-test repro 2026-05-25 spec=
        # 483 Test F). The endpoint's apply path opportunistically
        # uses deltas when the LLM happens to emit them, but the
        # legacy free-form fallback gives the LLM a familiar shape.
        return _OUTER_SYSTEM_PROMPT
    # Inner repair only edits scripts, never the Dockerfile, so the
    # FROM-locking rule is a no-op for inner. Same prompt either way.
    return _INNER_SYSTEM_PROMPT


_PLAN_DELTA_OUTER_SYSTEM_PROMPT = """You are a repair assistant for the MOD \
wizard node builder. The current spec carries a structured BuildPlan; \
your job is to propose 1-3 FIELD changes to that plan, which the system \
will then use to re-render the Dockerfile. You DO NOT write Dockerfile \
text — the renderer does that mechanically from the updated plan.

CRITICAL — do not add Docker-related anything:
The runtime container will never call Docker. Never propose deltas that \
install docker.io / docker-ce / docker-py, add a `docker` group, or \
reference /var/run/docker.sock. If you see error text containing \
"docker"/"daemon"/"socket", that's almost always the OUTER build daemon, \
NOT something your container needs.

CRITICAL — `USER mod` is provisioned automatically:
The renderer emits an idempotent `id -u mod || useradd ... || adduser ...` \
before `USER mod`. Don't propose deltas that touch `useradd -g docker mod` \
(corruption) — but plain `useradd mod` for non-curated bases is fine.

Other constraints:
1. Prefer adding packages / system deps to changing the base image.
2. Pin Python package versions to releases that exist on PyPI.
3. Keep edits minimal — 1-3 deltas total. If the failure needs more, \
   pick the most impactful three and let the next iteration handle \
   the rest.
4. Return JSON only, schema below. No prose outside `explanation`.

Response schema:
{
  "explanation": "<one short sentence on what you changed and why>",
  "deltas": [
    {"field": "<plan field>", "value": <new value>, "reason": "<one line>"}
  ],
  "rationale": "<optional longer-form context>"
}

Allowed `field` values (everything else is REJECTED):
- `install_steps` (list[str]) — pip/npm/apt-get/etc. invocations
- `system_deps` (list[str]) — apt package names
- `language` (str) — python | node | typescript | ruby | go | rust | java | shell
- `language_version` (str) — e.g. "3.11", "20"
- `package_manager` (str) — pip | poetry | uv | npm | yarn | pnpm | cargo | go-mod | bundle | none
- `env_vars` (list[{name, default}])
- `ports` (list[int])
- `entrypoint` (list[str])
- `workdir` (str) — absolute path
- `from_image_ref` (str) — only if the failure clearly requires a base swap
- `cuda_version` (str) — "MAJOR.MINOR" or null
- `gpu_required` (bool)
- `script_body` (str) — full replacement script body

Return ONLY the JSON object — no markdown fences, no prose."""


def is_image_mode_spec(spec_data: dict) -> bool:
    """Detect Image-mode submissions from a BuildSpec.

    Image mode = user picked a Docker image (wrap_base_image) and
    nothing else. No inline script body, no source repo. The §2
    adapter synthesizes the Dockerfile directly without the
    Containerizer LLM, so the FROM line is the user's choice and
    should not be touched by repair LLMs.
    """
    if not isinstance(spec_data, dict):
        return False
    wrap = (spec_data.get("wrap_base_image") or "").strip()
    inline = (spec_data.get("inline_source_body") or "").strip()
    repo = (spec_data.get("source_repo_url") or "").strip()
    extras = (spec_data.get("additional_instructions") or "").strip()
    return bool(wrap) and not inline and not repo and not extras


# ---------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------


_FORBIDDEN_PATH_PREFIXES = (
    "/etc/", "/usr/", "/var/", "/bin/", "/sbin/", "/root/",
    "/home/", "/run/", "/proc/", "/sys/",
)


# ---------------------------------------------------------------------
# Base-image rails — refuse GPU bases when the user asked for CPU
# ---------------------------------------------------------------------
#
# Stage repro: user described an image-format converter and the LLM
# picked `nvidia/cuda:12.4-base` as the FROM. The runtime printed
# "WARNING: The NVIDIA Driver was not detected" and fell back to CPU
# anyway — but the image was ~3 GB instead of ~150 MB and the cold-pull
# took minutes. The user explicitly asked for a CPU node; the wizard
# shouldn't override that with a heavy GPU base "just in case."

# Substring patterns in the FROM line that mark a base as GPU-flavored.
# We match on substring (not full string equality) so registry prefixes
# and tag suffixes don't break detection.
_GPU_BASE_MARKERS = (
    "nvidia/cuda",     # docker hub
    "nvcr.io/nvidia",  # NGC
    "rocm/",           # AMD
    "rocm:",
    "intel/intel-extension",  # XPU
    "intel/oneapi",
    "openvino/",       # OpenVINO (Intel inference)
    "tensorrt",
)


def extract_from_line(dockerfile_text: str) -> Optional[str]:
    """Return the value after the first `FROM` directive, or None."""
    if not dockerfile_text:
        return None
    for raw_line in dockerfile_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", line, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def is_gpu_flavored_base(from_image: str) -> bool:
    """Heuristic: does this base image bake in GPU libraries?"""
    if not from_image:
        return False
    lower = from_image.lower()
    return any(marker in lower for marker in _GPU_BASE_MARKERS)


def validate_base_image_for_process_type(
    dockerfile_text: str, process_type: str,
) -> List[str]:
    """Refuse Dockerfiles whose base contradicts the declared process_type.

    Returns rejection reasons (empty list = clean). The wizard sets
    process_type during describe-chat — CPU / CUDA / ROCm / INTEL.
    If the user said CPU and the LLM picked an nvidia/cuda base, we
    refuse and let the repair loop try again with explicit guidance.
    """
    process = (process_type or "").strip().upper() or "CPU"
    from_image = extract_from_line(dockerfile_text)
    if not from_image:
        return ["Dockerfile has no FROM directive"]
    gpu = is_gpu_flavored_base(from_image)
    if process == "CPU" and gpu:
        return [
            f"base image '{from_image}' is GPU-flavored but process_type=CPU. "
            "Pick a CPU base (e.g. python:3.11-slim, debian:bookworm-slim, "
            "ubuntu:22.04). Adding 'just in case' GPU libraries to a CPU "
            "node bloats the image by ~3 GB with no runtime benefit."
        ]
    # We only block CPU→GPU. CUDA/ROCm requested but LLM picked CPU is
    # allowed — the wizard would catch missing CUDA libs at runtime via
    # the inner repair loop (ModuleNotFoundError trigger → outer mode).
    return []


def validate_repair_response(
    mode: str,
    parsed: Dict[str, object],
) -> List[str]:
    """Return a list of rejection reasons for an LLM repair response.

    Empty list = clean response. Used by the API endpoint to refuse
    obviously-bad responses (path-traversal, missing fields, etc.)
    before they update the BuildSpec.
    """
    rejections: List[str] = []
    if not isinstance(parsed, dict):
        return ["response is not a JSON object"]

    explanation = parsed.get("explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        rejections.append("missing or empty 'explanation' field")

    scripts = parsed.get("updated_scripts")
    if scripts is not None:
        if not isinstance(scripts, dict):
            rejections.append("'updated_scripts' must be an object")
        else:
            for fname, content in scripts.items():
                if not isinstance(fname, str) or not fname:
                    rejections.append(f"script filename must be non-empty string, got {fname!r}")
                    continue
                if any(fname.startswith(p) for p in _FORBIDDEN_PATH_PREFIXES):
                    rejections.append(
                        f"script filename {fname!r} writes to a forbidden system path"
                    )
                if ".." in fname:
                    rejections.append(f"script filename {fname!r} contains '..'")
                if not isinstance(content, str):
                    rejections.append(f"script {fname!r} content must be a string")
                    continue
                # AST-validate Python sources before accepting. Stops
                # the LLM-thrash loop where inner repair returns a
                # script with a missing paren / smart-quote / etc.,
                # the build executes it, exits 2, then the next
                # repair attempt produces another broken script
                # (manual-test repro 2026-05-23 spec=317 Test F: 5
                # inner repairs all returning `f.write(...upper()`
                # missing a `)`).
                if fname.endswith(".py"):
                    try:
                        import ast as _ast
                        _ast.parse(content)
                    except SyntaxError as exc:
                        rejections.append(
                            f"script {fname!r} fails Python syntax check: "
                            f"{exc.msg!r} at line {exc.lineno}, offset {exc.offset}"
                        )

    if mode == "outer":
        # wizard-rebuild §7b — accept EITHER plan-delta shape OR the
        # legacy free-form Dockerfile rewrite. The endpoint's apply
        # path picks plan-delta when deltas is present + the spec has
        # a BuildPlan; this validator only checks shape sanity, not
        # which path the endpoint will take.
        deltas = parsed.get("deltas")
        dockerfile = parsed.get("updated_dockerfile")
        has_deltas = isinstance(deltas, list) and len(deltas) > 0
        has_dockerfile = isinstance(dockerfile, str) and dockerfile.strip()
        if not (has_deltas or has_dockerfile):
            rejections.append(
                "outer mode requires either 'deltas' (plan-delta shape) "
                "or 'updated_dockerfile' (legacy free-form shape)"
            )
        # When BOTH shapes are present (LLM hedged), the endpoint's
        # apply path prefers deltas + re-renders the Dockerfile —
        # the updated_dockerfile is discarded. Skip its FROM /
        # corruption checks in that case so the LLM doesn't get
        # rejected for a malformed Dockerfile we weren't going to
        # use anyway (manual-test repro 2026-05-25 spec=426 Test E:
        # validator rejected for "no FROM" twice while the deltas
        # were valid; thrash-bail fired).
        if has_dockerfile and not has_deltas:
            if "FROM " not in dockerfile:
                rejections.append("'updated_dockerfile' does not contain a FROM directive")
            else:
                rejections.extend(_reject_docker_corruption(dockerfile))
        if has_deltas:
            # Shape sanity only — the endpoint's apply_deltas does the
            # full per-field allowlist + consistency check + Pydantic
            # round-trip. We just check the list shape here so an
            # obvious typo (deltas: "string") gets surfaced early.
            from node_builder.build_plan.deltas import (
                MAX_DELTAS_PER_RESPONSE, MIN_DELTAS_PER_RESPONSE,
            )
            if len(deltas) < MIN_DELTAS_PER_RESPONSE:
                rejections.append(
                    f"'deltas' must have at least {MIN_DELTAS_PER_RESPONSE} entry"
                )
            if len(deltas) > MAX_DELTAS_PER_RESPONSE:
                rejections.append(
                    f"'deltas' has {len(deltas)} entries; max "
                    f"{MAX_DELTAS_PER_RESPONSE} per response"
                )
            for i, d in enumerate(deltas):
                if not isinstance(d, dict):
                    rejections.append(f"deltas[{i}] must be an object")

    return rejections


# Patterns the LLM repeatedly tries to add when it mis-attributes a
# build error to "Docker socket / DinD". None of these are ever
# correct: the build is already inside docker, runtime containers
# don't need docker, and the curated base images already define a
# non-root `mod` user. Reject them server-side so an erratic LLM
# response gets a retry with explicit guidance.
_DOCKER_CORRUPTION_PATTERNS = [
    # `apt-get install docker.io / docker-ce / docker-ce-cli` (and variants)
    (re.compile(r"\b(?:apt-get|apt)\s+install[^\n]*\b(docker\.io|docker-ce|docker-ce-cli|containerd\.io|docker-buildx-plugin)\b", re.IGNORECASE),
     "installs docker daemon/CLI inside the container — never needed; the build is already running in docker"),
    # `pip install docker / docker-py / docker-compose / dockerpy-creds`
    (re.compile(r"\bpip\s+install[^\n]*\b(docker(?:-py)?(?:==|\s|$)|docker-compose|dockerpy-creds)", re.IGNORECASE),
     "installs the docker python SDK — runtime containers never call the docker API"),
    # `groupadd docker` / `useradd ... -G docker / -g docker`
    (re.compile(r"\b(?:groupadd|addgroup)\s+(?:[-]g\s+\d+\s+)?docker\b", re.IGNORECASE),
     "groupadd docker — not needed; the runtime worker mounts no docker.sock for user containers"),
    # NOTE: the broad `useradd|adduser ... mod` pattern used to live
    # here on the (now-broken) assumption that curated bases always
    # provision uid 10001 as `mod`. The renderer now emits an
    # idempotent `id -u mod || useradd -m -u 10001 mod || adduser -D -u 10001 mod`
    # for arbitrary user bases (python:3.11-slim, ubuntu, alpine) so
    # `USER mod` at the bottom doesn't trip docker exec with "no
    # matching entries in passwd file". A blanket reject blocked the
    # repair LLM from returning a Dockerfile with the renderer's own
    # useradd intact (test-D / test-E manual repro 2026-05-23).
    #
    # Keep the narrower catch: `useradd ... -g/-G docker mod` IS still
    # corruption (the LLM trying to put mod in the docker group to
    # "fix" daemon access — never relevant for runtime nodes).
    (re.compile(r"\b(?:useradd|adduser)\s+[^\n]*\s-[gG]\s+docker[^\n]*\bmod\b", re.IGNORECASE),
     "useradd mod -g/-G docker — puts mod in the docker group; runtime nodes never need docker daemon access"),
    # `touch /var/run/docker.sock` or similar
    (re.compile(r"/var/run/docker\.sock\b"),
     "touches/references /var/run/docker.sock — runtime containers have no docker socket and don't need one"),
    # docker-in-docker setups
    (re.compile(r"\bdocker:dind\b|\bdocker-in-docker\b", re.IGNORECASE),
     "docker-in-docker setup — wizard nodes never need DinD"),
]


def _reject_docker_corruption(dockerfile_text: str) -> List[str]:
    """Catch LLM responses that fall into the docker-fixation thrashing
    pattern from spec 12/13 (TRELLIS.2 live runs). The repair endpoint
    refuses these responses and the loop retries; the next iteration's
    prior_attempts surface the rejection so the LLM can pivot."""
    rejections: List[str] = []
    if not dockerfile_text:
        return rejections
    for pat, reason in _DOCKER_CORRUPTION_PATTERNS:
        if pat.search(dockerfile_text):
            rejections.append(
                f"Dockerfile contains a docker-related corruption pattern that "
                f"never fixes wizard build failures: {reason}. Remove it; the "
                f"real failure is something else (look at the actual stderr "
                f"in the failure_details, not the word 'docker' that appears "
                f"incidentally in build output)."
            )
    return rejections
