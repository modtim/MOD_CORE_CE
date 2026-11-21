"""Guard against banned ML provider names sneaking back into the
Containerizer's defaults.

Policy: shipped ML defaults must not reference banned model providers.

This test scans every default model-name string in the Containerizer
code and fails if any matches the banned list. Catches the failure
mode that already happened once: a refactor reintroduced
`BAAI/bge-small-en-v1.5` as a default and shipped to stage before
anyone noticed.

Override values from env vars (CODEBASE_EMBED_MODEL=BAAI/...) are
NOT caught here — those are operator-set at runtime and out of scope.
Only the CODE DEFAULTS are policy-bound.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


# Patterns that indicate a banned provider as a code default. Matches
# org-prefixed model names (the HuggingFace convention) in source.
# String "BAAI" is overly broad on its own — narrow to the slash form.
_BANNED_PATTERNS = [
    re.compile(r'["\']BAAI/[A-Za-z0-9_.-]+["\']'),
    re.compile(r'["\']Alibaba(-NLP)?/[A-Za-z0-9_.-]+["\']'),
    re.compile(r'["\']Qwen/[A-Za-z0-9_.-]+["\']'),
    re.compile(r'["\']Tencent/[A-Za-z0-9_.-]+["\']'),
    re.compile(r'["\']deepseek-ai/[A-Za-z0-9_.-]+["\']', re.IGNORECASE),
    re.compile(r'["\']THUDM/[A-Za-z0-9_.-]+["\']'),
    re.compile(r'["\']ai-forever/[A-Za-z0-9_.-]+["\']'),  # Sber (Russia)
    re.compile(r'["\']sberbank-ai/[A-Za-z0-9_.-]+["\']'),
]

# Files that are PERMITTED to mention banned names — typically
# documentation, migration comments explaining "we used to use X,
# now use Y", or test fixtures that exercise the rejection path.
_ALLOWED_REFERENCES = {
    # This test file itself.
    str(Path(__file__).resolve()),
}


def _iter_source_files():
    for path in _SCRIPTS_DIR.rglob("*.py"):
        if str(path) in _ALLOWED_REFERENCES:
            continue
        # Skip __pycache__ and virtualenvs that might've snuck in.
        if "__pycache__" in path.parts or ".venv" in path.parts:
            continue
        yield path


def test_no_banned_ml_provider_defaults_in_source():
    """Every .py under scripts/ must not contain a banned provider's
    model name as a string literal. Comments mentioning the legacy
    BAAI choice in migration notes are fine — they're explanatory
    text, not defaults."""
    violations: list[tuple[Path, int, str, str]] = []

    for path in _iter_source_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            # Comments are explanatory and allowed to reference
            # banned names (e.g. "switched from BAAI to Snowflake").
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            for pattern in _BANNED_PATTERNS:
                m = pattern.search(line)
                if m:
                    violations.append((path, lineno, m.group(0), line.strip()))

    if violations:
        details = "\n".join(
            f"  {path}:{lineno}: {match} → {line[:120]}"
            for path, lineno, match, line in violations
        )
        pytest.fail(
            "Banned ML provider model names found as code defaults:\n"
            + details
            + "\n\nPolicy: aligned providers only (Snowflake / Microsoft / "
            "Nomic / Mixedbread / UKP-Lab)."
        )


def test_embed_index_default_is_aligned_provider():
    """Pin the specific default we swapped to. If a future refactor
    silently restores 'BAAI/bge-small-en-v1.5', this test catches it
    before the build ships. Source-pin (reads the file directly)
    rather than importing — the test environment doesn't carry the
    Containerizer's full dep tree."""
    embed_index_src = (_SCRIPTS_DIR / "tools" / "embed_index.py").read_text()
    # The default value lives in the env-var fallback; locate it
    # specifically so an unrelated mention in a comment doesn't pass.
    assert 'CODEBASE_EMBED_MODEL", "Snowflake/snowflake-arctic-embed-s"' in embed_index_src, (
        "default embed model must be Snowflake/snowflake-arctic-embed-s "
        "(aligned-provider policy). Was BAAI/bge-small-en-v1.5 before "
        "the swap — don't reintroduce it."
    )


def test_allow_list_test_files_can_reference_banned_names():
    """Sanity: tests that pin the rejection behavior themselves
    contain banned names. The allow-list lets them through."""
    # This test file itself contains "BAAI/" etc. in patterns — if the
    # allow-list weren't applied, test_no_banned_ml_provider_defaults_in_source
    # would fail on its own siblings. Hence the explicit ALLOWED set.
    assert str(Path(__file__).resolve()) in _ALLOWED_REFERENCES
