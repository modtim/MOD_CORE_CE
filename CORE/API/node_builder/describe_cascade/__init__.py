"""wizard-rebuild §8 — Describe-mode cascade.

The cascade picks the cheapest path that satisfies the user's
natural-language description of what their node should do. Steps run
in order (each can short-circuit if it produces a high-confidence
candidate):

  A. Container search — look for a pre-built Docker image on Docker
     Hub, HuggingFace Spaces, or NGC that already does this thing.
     Cheapest because no build is needed — Image-mode wraps it.
  B. Repo search — wrap the existing /describe-search endpoint to
     find a GitHub repo. Picked candidate flows into docs-driven
     Git mode (§3 + §4).
  C. Library search — LLM names PyPI / npm libraries that solve the
     intent; existence is verified via the registry's metadata API.
     Picked library flows into Script mode with a thin wrapper that
     imports it.
  D. Codegen — last resort. Containerizer LLM authors a Script-mode
     wrapper from scratch (existing `describe_synthesize` helper).

The orchestrator (`cascade.run_cascade`) walks the steps; each step
returns ranked candidates with a confidence score. The cascade
short-circuits on the first step that produces a candidate above the
configured floor (default 0.7), so a clear "use the existing
ffmpeg container" prompt doesn't waste an LLM call on codegen.

Per-backend rate limits are tracked separately (`rate_limits` module)
because the user-facing "you've hit your limit" message wants to be
specific (Docker Hub is rate-limited, not HF Spaces, not GitHub).
Cache hits don't count against the window — searches are
deterministic enough that re-asking the same question shouldn't burn
quota.
"""
