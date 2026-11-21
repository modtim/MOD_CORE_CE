"""δ Phase 1 — single-stop repo safety evaluator.

Run on every repo URL the wizard touches:
- describe-source: each candidate from GitHub search (Phase 6)
- repo-source: the user-pasted git URL (Phase 11)

Combines license check + GitHub-flagged repo state + a small
keyword block for obvious abuse-vector descriptions. Returns a
structured result so the wizard can render specific user-facing
reasons instead of a generic 'rejected'.

All checks operate on a normalised metadata dict (the shape
github_search.fetch_repo_metadata returns), not on raw GitHub
JSON, so this module stays free of GitHub-specific quirks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from utils import license_allowlist


# Heuristic keyword block on the repo description. Not bulletproof
# — a determined user can use a misleading description — but
# refuses the obvious cases (cryptocurrency mining, exploit kits,
# credential harvesters) before we burn an LLM call scoring them.
# Keywords are matched case-insensitively as substrings.
_BLOCKED_DESCRIPTION_KEYWORDS: List[str] = [
    "cryptocurrency miner",
    "crypto miner",
    "monero miner",
    "xmrig",
    "ddos tool",
    "ddos attack",
    "exploit kit",
    "rat malware",
    "credential harvester",
    "credential stealer",
    "rootkit",
    "keylogger",
    "ransomware",
    "botnet",
    "phishing kit",
    "stresser",
    "spam tool",
    "wallet stealer",
]


@dataclass
class RepoSafetyResult:
    ok: bool
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    license_spdx: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "license_spdx": self.license_spdx,
        }


def _check_license(metadata: Dict[str, Any], result: RepoSafetyResult) -> None:
    spdx = metadata.get("license_spdx")
    result.license_spdx = spdx
    if not license_allowlist.is_allowed(spdx):
        result.ok = False
        result.reasons.append(license_allowlist.block_reason(spdx))


def _check_repo_flags(metadata: Dict[str, Any], result: RepoSafetyResult) -> None:
    if metadata.get("archived"):
        result.ok = False
        result.reasons.append(
            "Repository is archived. Pick a maintained alternative — "
            "archived repos won't receive security fixes."
        )
    if metadata.get("disabled"):
        result.ok = False
        result.reasons.append(
            "Repository has been disabled by GitHub (typically due to "
            "an abuse / DMCA flag). Pick a different repository."
        )
    # GitHub flags repos for malicious content via the abuse-disabled
    # flag; surface as outright rejection.
    if metadata.get("is_template") and metadata.get("size", 0) == 0:
        # Empty template; not actionable for our use case.
        result.warnings.append(
            "Repository looks like an empty template (zero size). "
            "It probably won't produce a useful node."
        )


def _check_description_keywords(metadata: Dict[str, Any], result: RepoSafetyResult) -> None:
    description = (metadata.get("description") or "").lower()
    if not description:
        return
    for kw in _BLOCKED_DESCRIPTION_KEYWORDS:
        if kw in description:
            result.ok = False
            result.reasons.append(
                f"Repository description matches a blocked-content "
                f"keyword ({kw!r}). MOD Core does not build images "
                "for tools whose primary purpose is abuse / malware / "
                "unauthorised access."
            )
            return  # one reason is enough


def _check_activity_warning(metadata: Dict[str, Any], result: RepoSafetyResult) -> None:
    """Soft warning — does NOT flip ok=False. Stale / tiny repos
    are still buildable, but the wizard should surface the risk so
    the user can pick a better candidate."""
    stars = metadata.get("stars") or 0
    if stars < 5:
        result.warnings.append(
            "Repository has fewer than 5 stars. The wrapper script "
            "may be the only consumer, so undiscovered bugs are "
            "more likely."
        )
    last_pushed = metadata.get("last_pushed_iso") or ""
    # Crude staleness heuristic — anything without a 2024-or-newer
    # ISO prefix is flagged. The caller can refine if it has a
    # better timestamp comparator handy.
    if last_pushed and not (last_pushed.startswith("2024") or
                            last_pushed.startswith("2025") or
                            last_pushed.startswith("2026")):
        result.warnings.append(
            f"Repository was last updated in {last_pushed[:7]} — "
            "it may be unmaintained."
        )


def evaluate_repo(metadata: Dict[str, Any]) -> RepoSafetyResult:
    """Run every safety check against a single repo's metadata.

    Returns ``RepoSafetyResult`` with ``ok`` set to False if any
    hard check fails (licence, archived, disabled, keyword block).
    Soft warnings (stale, tiny) are appended to ``warnings`` but
    do not flip the gate.

    Caller convention: a failed result is a 422 to the user with
    ``reasons`` rendered as a bullet list. Warnings render as a
    yellow admonition above the candidate row.
    """
    result = RepoSafetyResult(ok=True)
    _check_license(metadata, result)
    _check_repo_flags(metadata, result)
    _check_description_keywords(metadata, result)
    _check_activity_warning(metadata, result)
    return result
