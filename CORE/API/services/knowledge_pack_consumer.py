"""services/knowledge_pack_consumer.py -- wizard-knowledge-pack
(`the internal design notes` Phase 2, tasks 2.1-2.4):
signature-verified consumer of MOD-published knowledge packs.

Pipeline mirrors `design.md:40-55` exactly:
    fetch_manifest -> verify_signature -> download_pack ->
    verify_content_hash -> apply_pack (upsert + integrity checks,
    commit or rollback) -- the router (986e, `POST
    /wizard/update-knowledge`) owns stitching these together; this module
    only implements each step plus the ledger bookkeeping.

Signature verification (2.1) reuses `services/output_signature.py`'s
Ed25519 pattern (try/except `InvalidSignature`, fail-closed, never raise
to the caller) but verifies against a PUBLIC key baked in via
`wizard_knowledge_settings.wizard_knowledge_pack_pubkey()` (986c) instead
of a per-install-minted private key's derived public half.

`apply_pack` targets ONLY the LIVE `LibraryProfile` columns --
`gpu_required`/`evidence`/`install_docs_excerpt`/`install_docs_fetched_at`
plus the new `source`/`pack_version` columns (986a). It never writes
`install_recipe_by_accel`/`successful_build_count_by_accel` -- those are
dead since GB.LEAK.5.

Source-priority guard: reuses `services/library_profiler._source_priority`
(986b, landed as ef2611d66) rather than duplicating the priority table.
Semantics match `design.md:60-64`: manual > local_capture > mod_pack; a
mod_pack upsert never touches a higher-priority existing row (equal
priority refreshes it).

Pack fetches (`fetch_manifest`/`download_pack`) go through
`utils.safe_http` (the platform's SSRF-guarded egress gate), reading the
platform allowlist via an explicit caller-supplied `db` session rather
than opening a second, untracked `SessionLocal()` internally.

Rollback (`rollback_pack`) is LEDGER-ONLY: pack bodies are not retained
post-apply (`design.md:122`), so a rollback can only record that a
version was rolled back -- it never restores `library_profiles`/
denylist/prompt-override/failure-hint rows a since-applied pack wrote
(and, per the priority guard above, a mod_pack row can never have
overwritten a higher-priority manual/local_capture row in the first
place, so there is nothing of that kind to restore). See its docstring.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

import models
from services import platform_settings as psr
from services import wizard_knowledge_settings as wks
from services.library_profiler import _source_priority
from utils import safe_http

logger = logging.getLogger(__name__)

try:
    from cryptography.hazmat.primitives.asymmetric import ed25519 as _ed25519
    from cryptography.exceptions import InvalidSignature
    _ED25519_AVAILABLE = True
except ImportError:  # pragma: cover -- CE builds without cryptography
    _ed25519 = None
    InvalidSignature = None
    _ED25519_AVAILABLE = False

_FETCH_TIMEOUT_S = 10.0
_SHELL_METACHARS = ("`", "$(", ";", "|", "&&")
_MOD_PACK_SOURCE = "mod_pack"
_MOD_PACK_PRIORITY = _source_priority(_MOD_PACK_SOURCE)


class KnowledgePackVersionError(Exception):
    """Base for apply_pack's version-gate rejections. The router (986e)
    maps both subclasses below to HTTP 409, reporting which case it was."""


class KnowledgePackDowngradeError(KnowledgePackVersionError):
    """`version` < the currently-applied version."""


class KnowledgePackReplayError(KnowledgePackVersionError):
    """`version` == the currently-applied version (idempotent no-op)."""


# ---------------------------------------------------------------------------
# 2.1 -- fetch / verify / download (fail-closed, never raise to the caller)
# ---------------------------------------------------------------------------

def _platform_allowlist(db: Session) -> list:
    """The `security.ssrf_allowlist` platform setting, read on the
    caller's own `db` session (never opens a second `SessionLocal()` the
    way `safe_http`'s internal default-allowlist path would)."""
    raw = psr.get_setting(safe_http.PLATFORM_ALLOWLIST_KEY, db=db)
    return [str(x) for x in raw] if isinstance(raw, (list, tuple)) else []


def fetch_manifest(url: str, db: Session) -> Optional[dict]:
    """GET `latest.json` through the platform's SSRF-guarded egress gate
    (`utils.safe_http`, allowlist read on the caller's `db` session).
    Best-effort: connection errors / non-200 / a non-dict body / an
    egress refusal all return None (Phase 3's air-gap detection reuses
    this "unreachable" signal -- it is not an error path, just "no
    update")."""
    try:
        resp = safe_http.get(url, _ssrf_allowlist=_platform_allowlist(db),
                             _ssrf_connect_timeout=_FETCH_TIMEOUT_S,
                             _ssrf_read_timeout=_FETCH_TIMEOUT_S)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception as e:  # noqa: BLE001 -- unreachable is a normal outcome
        logger.info("knowledge_pack_consumer: manifest fetch failed for %s: %s",
                   url, e)
        return None


def _manifest_message(manifest: dict) -> bytes:
    """The exact bytes the generator signs: `"{version}:{checksum}"`. Only
    these two fields are authenticated -- `source_url`/`signature` are
    transport metadata, not generator-authored content."""
    version = str(manifest.get("version", ""))
    checksum = str(manifest.get("checksum", ""))
    return f"{version}:{checksum}".encode("utf-8")


def verify_signature(manifest: dict, pubkey_b64: str) -> bool:
    """Ed25519 verify of `manifest`'s signed fields against
    `wizard_knowledge_settings.wizard_knowledge_pack_pubkey()`. False on
    any missing/empty/malformed input or a bad signature -- never raises."""
    if not _ED25519_AVAILABLE or not pubkey_b64 or not manifest:
        return False
    signature_b64 = manifest.get("signature")
    if not signature_b64:
        return False
    try:
        pubkey = _ed25519.Ed25519PublicKey.from_public_bytes(
            base64.b64decode(pubkey_b64))
        pubkey.verify(base64.b64decode(signature_b64),
                      _manifest_message(manifest))
        return True
    except InvalidSignature:
        return False
    except Exception as e:  # noqa: BLE001 -- malformed b64/key is a failed verify
        logger.warning("knowledge_pack_consumer: signature verify error: %s", e)
        return False


def download_pack(source_url: str, db: Session) -> bytes:
    """Fetch the pack body bytes through the same SSRF-guarded egress gate
    as `fetch_manifest` (allowlist read on the caller's `db` session).
    Fail-closed sentinel: b"" on any connection error / non-200 / egress
    refusal (never raises -- callers check truthiness, matching
    `verify_content_hash`'s `not pack_bytes` guard)."""
    try:
        resp = safe_http.get(source_url, _ssrf_allowlist=_platform_allowlist(db),
                             _ssrf_connect_timeout=_FETCH_TIMEOUT_S,
                             _ssrf_read_timeout=_FETCH_TIMEOUT_S)
        if resp.status_code != 200:
            return b""
        return resp.content
    except Exception as e:  # noqa: BLE001
        logger.info("knowledge_pack_consumer: pack download failed for %s: %s",
                   source_url, e)
        return b""


def verify_content_hash(pack_bytes: bytes, expected_checksum: str) -> bool:
    """sha256(pack_bytes) == expected_checksum (case-insensitive hex)."""
    if not pack_bytes or not expected_checksum:
        return False
    actual = hashlib.sha256(pack_bytes).hexdigest()
    return actual.lower() == str(expected_checksum).strip().lower()


# ---------------------------------------------------------------------------
# 2.2/2.3 -- apply (upsert + append-only settings merges) + 2.4 integrity
# ---------------------------------------------------------------------------

def _version_num(version: str) -> int:
    """`vNNN` (tasks.md 1.2) -> int. Unparseable input -> 0 (treated as the
    lowest possible version, so a malformed string never beats a real one)."""
    v = str(version).strip().lower()
    if v.startswith("v"):
        v = v[1:]
    try:
        return int(v)
    except ValueError:
        return 0


def _latest_applied_version(db: Session) -> Optional[str]:
    row = (
        db.query(models.WizardKnowledgePackVersion)
        .filter(models.WizardKnowledgePackVersion.status == "applied")
        .order_by(models.WizardKnowledgePackVersion.id.desc())
        .first()
    )
    return row.version if row is not None else None


def _contains_shell_metachars(text: Optional[str]) -> bool:
    if not text:
        return False
    return any(tok in text for tok in _SHELL_METACHARS)


def _upsert_library_profiles(db: Session, entries: List[Dict[str, Any]],
                             version: str) -> List[models.LibraryProfile]:
    """Idempotent UPSERT keyed on `name`. Uses `library_profiler._source_priority`
    (986b) -- never downgrades an existing `manual`/`local_capture` row
    (`design.md:80-86`); an equal-priority existing row (another
    `mod_pack` write) is refreshed. Returns the rows this call actually
    touched."""
    touched: List[models.LibraryProfile] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        gpu_required = entry.get("gpu_required")
        if not name or not gpu_required:
            continue
        existing = db.get(models.LibraryProfile, name)
        if (existing is not None
                and _source_priority(existing.source) > _MOD_PACK_PRIORITY):
            continue  # manual/local_capture outranks mod_pack -- skip
        if existing is None:
            existing = models.LibraryProfile(name=name)
            db.add(existing)
        existing.gpu_required = gpu_required
        existing.evidence = entry.get("evidence")
        excerpt = entry.get("install_docs_excerpt")
        if excerpt is not None:
            existing.install_docs_excerpt = excerpt
            existing.install_docs_fetched_at = datetime.now(timezone.utc)
        existing.source = _MOD_PACK_SOURCE
        existing.pack_version = version
        touched.append(existing)
    db.flush()
    return touched


def _append_denylist(db: Session, new_entries: List[str],
                     actor_user_id: Optional[int]) -> None:
    """Append-only: existing entries (including manually-curated ones)
    are never dropped, only deduped against."""
    if not new_entries:
        return
    current = wks.denylist_additions(db)
    merged = list(current)
    seen = set(current)
    for entry in new_entries:
        if isinstance(entry, str) and entry.strip() and entry not in seen:
            merged.append(entry)
            seen.add(entry)
    if merged != current:
        psr.set_setting(wks.DENYLIST_ADDITIONS_KEY, merged, db=db,
                        actor_user_id=actor_user_id)


def _merge_prompt_overrides(db: Session, pack_overrides: Dict[str, str],
                            actor_user_id: Optional[int]) -> None:
    """UPSERT: pack keys are added/updated, unrelated existing keys are
    left untouched (no destructive whole-dict overwrite)."""
    if not pack_overrides or not isinstance(pack_overrides, dict):
        return
    current = dict(wks.prompt_overrides(db))
    merged = dict(current)
    for k, v in pack_overrides.items():
        if isinstance(k, str) and isinstance(v, str):
            merged[k] = v
    if merged != current:
        psr.set_setting(wks.PROMPT_OVERRIDES_KEY, merged, db=db,
                        actor_user_id=actor_user_id)


def _merge_failure_hints(db: Session, pack_hints: List[Dict[str, str]],
                         actor_user_id: Optional[int]) -> None:
    """UPSERT keyed on `stderr_regex`: pack entries add/replace by key,
    unrelated existing hints are kept."""
    if not pack_hints:
        return
    current = list(wks.failure_hints(db))
    by_regex = {h.get("stderr_regex"): h for h in current if isinstance(h, dict)}
    changed = False
    for hint in pack_hints:
        if not isinstance(hint, dict):
            continue
        key = hint.get("stderr_regex")
        if not key:
            continue
        if by_regex.get(key) != hint:
            by_regex[key] = hint
            changed = True
    if changed:
        psr.set_setting(wks.FAILURE_HINTS_KEY, list(by_regex.values()), db=db,
                        actor_user_id=actor_user_id)


def _run_integrity_checks(touched: List[models.LibraryProfile],
                          declared_count: int) -> List[str]:
    """Task 2.4: row-count delta within +/-20% of the pack's declared
    count; all touched rows non-null name+gpu_required; no touched row's
    install_docs_excerpt contains shell-metacharacter sequences."""
    reasons: List[str] = []
    if declared_count > 0:
        delta = abs(len(touched) - declared_count) / declared_count
        if delta > 0.20:
            reasons.append(
                f"row-count delta {delta:.0%} exceeds 20% "
                f"(upserted={len(touched)}, declared={declared_count})")
    for row in touched:
        if not row.name or not row.gpu_required:
            reasons.append(f"touched row {row.name!r} missing name/gpu_required")
        if _contains_shell_metachars(row.install_docs_excerpt):
            reasons.append(
                f"install_docs_excerpt for {row.name!r} contains a "
                f"shell-metacharacter sequence")
    return reasons


class _IntegrityCheckFailed(Exception):
    def __init__(self, reasons: List[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def apply_pack(db: Session, pack: Dict[str, Any], version: str,
               actor_user_id: Optional[int] = None) -> Dict[str, Any]:
    """Apply one knowledge-pack body. Returns
    `{rows_upserted, integrity_status}` (`integrity_status` is `'applied'`
    or `'rolled_back'`, with `reasons` present on rollback).

    Raises `KnowledgePackDowngradeError`/`KnowledgePackReplayError` (the
    router maps both to 409) before touching any data if `version` is not
    strictly newer than the currently-applied version."""
    current_version = _latest_applied_version(db)
    if current_version is not None:
        current_num, new_num = _version_num(current_version), _version_num(version)
        if new_num == current_num:
            raise KnowledgePackReplayError(
                f"wizard-knowledge-pack {version!r} is already applied")
        if new_num < current_num:
            raise KnowledgePackDowngradeError(
                f"wizard-knowledge-pack {version!r} is older than the "
                f"currently-applied {current_version!r}")

    declared_count = pack.get("declared_row_count")
    if declared_count is None:
        declared_count = len(pack.get("library_profiles") or [])

    try:
        with db.begin_nested():
            touched = _upsert_library_profiles(
                db, pack.get("library_profiles") or [], version)
            _append_denylist(db, pack.get("hallucinated_bases") or [],
                             actor_user_id)
            _merge_prompt_overrides(
                db, pack.get("containerizer_prompt_cheatsheet") or {},
                actor_user_id)
            _merge_failure_hints(
                db, pack.get("failure_pattern_hints") or [], actor_user_id)

            reasons = _run_integrity_checks(touched, declared_count)
            if reasons:
                raise _IntegrityCheckFailed(reasons)
    except _IntegrityCheckFailed as exc:
        db.add(models.WizardKnowledgePackVersion(
            version=version,
            status="rollback",
            source_url=pack.get("source_url"),
            signature_status="valid",
            integrity_check_payload={"reasons": exc.reasons,
                                     "declared_row_count": declared_count},
        ))
        db.commit()
        return {"rows_upserted": 0, "integrity_status": "rolled_back",
               "reasons": exc.reasons}

    db.add(models.WizardKnowledgePackVersion(
        version=version,
        status="applied",
        applied_at=datetime.now(timezone.utc),
        applied_by_user_id=actor_user_id,
        signature_status="valid",
        source_url=pack.get("source_url"),
        integrity_check_payload={"rows_upserted": len(touched),
                                 "declared_row_count": declared_count},
    ))
    db.commit()
    return {"rows_upserted": len(touched), "integrity_status": "applied"}


def rollback_pack(db: Session, to_version: str) -> Dict[str, Any]:
    """Best-effort rollback to a prior applied pack version.

    SIMPLIFICATION (required given `design.md:122`: pack bodies are not
    retained post-apply): there is no stored pack body to replay
    library_profiles / denylist / prompt-override / failure-hint CONTENT
    from. This call can only re-verify `to_version` was previously
    `'applied'` in the ledger and write a new `'rollback'` ledger row
    pointing at it -- it does NOT revert data rows written by any
    since-applied newer pack version (that would need the newer pack's
    body, e.g. its `removed_library_profiles[]` diff per `design.md:105`,
    which this consumer never retains). A true data rollback means
    re-fetching `to_version`'s pack from the distribution endpoint and
    calling `apply_pack()` again as a fresh forward apply -- left to the
    caller (986e's router / an operator), not reimplemented here.
    """
    target = (
        db.query(models.WizardKnowledgePackVersion)
        .filter(models.WizardKnowledgePackVersion.version == to_version,
               models.WizardKnowledgePackVersion.status == "applied")
        .order_by(models.WizardKnowledgePackVersion.id.desc())
        .first()
    )
    if target is None:
        return {"rolled_back": False,
               "reason": f"version {to_version!r} has no prior 'applied' "
                        f"ledger entry"}
    db.add(models.WizardKnowledgePackVersion(
        version=to_version,
        status="rollback",
        source_url=target.source_url,
        signature_status=target.signature_status,
        integrity_check_payload={
            "rollback_of": to_version,
            "note": "ledger-only rollback -- pack body not retained "
                   "post-apply (design.md:122); library_profiles/"
                   "denylist/prompt-overrides/failure-hints content from "
                   "intervening pack versions is NOT reverted",
        },
    ))
    db.commit()
    return {"rolled_back": True, "version": to_version,
           "note": "ledger-only; data rows not reverted (see docstring)"}
