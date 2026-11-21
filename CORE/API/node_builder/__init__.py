"""Node-builder helpers — base-image detection and LLM prompt assembly.

Keeps the non-admission helpers (detection + prompt shaping) separate
from the admission gate so the latter can stay a pure-function policy
check without reaching into LLM code paths.
"""
