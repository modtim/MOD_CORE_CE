"""FX.117 p3a — exact-value masking of tenant secret VALUES.

FX.117 (owner Q104=A) ships the customer secret store. Part 1 masked secret
values in the worker TASK report (log_tail / outputs / error_message) with
`utils.secret_redaction.mask_exact_values`. This module is the standalone,
pure half of FX.117.p3a: the exact-value mask applied at the NODE-RUN ingest
point (`crud/node_run_cud.update_node_run_status`), i.e. what the API
persists in `NodeRun.inputs` / `NodeRun.outputs` / `NodeRun.error_message`
and therefore also serves back to the operator.

Design:
  * The mask is EXACT-VALUE, not pattern-based. `utils.secret_redaction.redact`
    only catches values that LOOK like a secret (password=..., Bearer ...,
    conn strings, JWTs). A node that echoes a bare credential — an opaque
    random string with no recognisable shape — persists it verbatim. Only
    knowledge of the run's own secret values closes that gap.
  * Mask token is the constant `MASK_TOKEN` ("***") — the same token part 1
    uses, so masked output looks identical wherever it was applied.
  * Values shorter than `MIN_MASK_LEN` (4) are skipped: a 3-char secret would
    redact ordinary prose ("the", "and", path fragments) and make logs
    unreadable. That threshold is the documented trade-off.
  * Encoded forms are masked too: a node that base64-encodes or URL-encodes
    the credential before printing it (a very common accident in shell/HTTP
    nodes) still must not persist it.
  * Longest-first, so one secret being a substring of another still masks the
    longer match (overlapping values).
  * Nothing here ever raises: masking is a storage nicety, it must never break
    the ingest that already happened on the worker side.
"""
from __future__ import annotations

import base64
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import models
from sqlalchemy.orm import Session

MASK_TOKEN = "***"
MIN_MASK_LEN = 4
MASKING_SETTING_KEY = "secrets.mask_values_in_logs"

# Per-run cache of the run's resolved secret values. The ingest point fires
# once per node-run status update (a fan-out run does this per file), so the
# graph walk + Vault decrypt is repeated N times per run otherwise. Values are
# short-lived plaintexts held for RUN_SECRET_CACHE_TTL_S seconds only, keyed by
# run id — the same values the resolver already hands the consuming node.
RUN_SECRET_CACHE_TTL_S = 15.0
_RUN_SECRET_CACHE: Dict[int, Tuple[float, List[str]]] = {}


def clear_run_secret_cache() -> None:
    """Drop every cached run-secret lookup (tests + explicit eviction)."""
    _RUN_SECRET_CACHE.clear()


def secret_value_forms(value: str) -> List[str]:
    """Every textual form of `value` that must be masked: the value itself
    plus its base64 (with and without padding), its URL/percent encoding,
    and — when the value itself looks like base64 — its decoded form.

    Forms shorter than MIN_MASK_LEN are dropped here (not just at the value
    level) so an encoded derivative can never become a 2-char false-positive
    magnet."""
    forms = {value}
    try:
        raw = value.encode("utf-8")
        enc = base64.b64encode(raw).decode("ascii")
        forms.add(enc)
        if enc.endswith("="):
            forms.add(enc.rstrip("="))
        forms.add(_percent_encode(raw))
    except Exception:  # noqa: BLE001 — an odd value still gets its plain form
        pass
    decoded = _maybe_base64_decode(value)
    if decoded:
        forms.add(decoded)
    return sorted(f for f in forms if isinstance(f, str) and len(f) >= MIN_MASK_LEN)


def _percent_encode(raw: bytes) -> str:
    """Percent-encoding of the value as it appears in a URL/query string.

    `urllib.parse.quote_plus` on the bytes: a credential pasted into a URL
    arrives percent-encoded, so the encoded spelling is masked as well."""
    from urllib.parse import quote_plus
    return quote_plus(raw.decode("latin-1", errors="replace"))


def _maybe_base64_decode(value: str) -> Optional[str]:
    """Decoded form of `value` ONLY when it is plausibly base64 text.

    Guards: length >= 8, pure base64 alphabet, and the decoded bytes must be
    printable ASCII. A random credential that happens to decode into binary
    garbage is not added — masking garbage would only create false positives.
    """
    if len(value) < 8 or len(value) % 4:
        return None
    if any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=" for c in value):
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception:  # noqa: BLE001
        return None
    try:
        text = raw.decode("ascii")
    except Exception:  # noqa: BLE001 — non-ASCII bytes: not a printable form
        return None
    if any(ord(c) < 32 or ord(c) > 126 for c in text):
        return None
    return text


def mask_secret_values(text, values: Iterable[str]) -> str:
    """Replace every exact occurrence of a secret VALUE (and its encoded
    forms) in `text` with `MASK_TOKEN`. Longest form first, so overlapping
    values mask the longer match. Values under MIN_MASK_LEN are skipped.

    Non-string `text` is returned unchanged (never raises): the ingest passes
    whatever the worker reported, and masking must not invent a value.
    """
    if not isinstance(text, str):
        return text
    forms = _forms_for(values)
    if not forms:
        return text
    out = text
    for form in forms:
        if form in out:
            out = out.replace(form, MASK_TOKEN)
    return out


def _forms_for(values: Iterable[str]) -> List[str]:
    """All masked forms of every candidate value, longest first."""
    forms = set()
    for v in (values or []):
        if isinstance(v, str) and len(v) >= MIN_MASK_LEN:
            forms.update(secret_value_forms(v))
    return sorted(forms, key=len, reverse=True)


def mask_in_obj(obj: Any, values: Iterable[str]) -> Any:
    """`mask_secret_values` applied through dicts/lists/tuples (the JSONB
    shapes the API stores: NodeRun.inputs / NodeRun.outputs).

    Dict KEYS are left alone — they are field names the platform wrote, never
    worker text. Structures are rebuilt only when something actually changed,
    so the common no-secret case returns the identical object."""
    forms = _forms_for(values)
    if not forms:
        return obj

    def _walk(node):
        if isinstance(node, str):
            return _mask_with_forms(node, forms)
        if isinstance(node, list):
            walked = [_walk(x) for x in node]
            return walked if any(a is not b for a, b in zip(walked, node)) else node
        if isinstance(node, tuple):
            walked = tuple(_walk(x) for x in node)
            return walked if walked != node else node
        if isinstance(node, dict):
            rebuilt = {k: _walk(v) for k, v in node.items()}
            return rebuilt if any(rebuilt[k] is not v for k, v in node.items()) else node
        return node

    return _walk(obj)


def _mask_with_forms(text: str, forms: List[str]) -> str:
    out = text
    for form in forms:
        if form in out:
            out = out.replace(form, MASK_TOKEN)
    return out


def masking_enabled(db: Session = None) -> bool:
    """The `secrets.mask_values_in_logs` platform setting (default True).

    Fail-safe toward masking: a settings-subsystem failure returns True, so a
    broken registry never silently disables redaction. (The values themselves
    come from a separate lookup, which degrades to [] on failure — masking
    can only be lost together with the values it protects.)

    FX.117 p4c — under a gov/DoD compliance profile (cmmc / fedramp_high_il5,
    the same list services/credential_lifetime.GOV_PROFILES uses) masking is
    FORCED ON: the setting cannot turn it off. The profile lock in
    services/compliance_profiles.py already refuses the platform-scope write
    that would clear it, and this is the read-side half of the same pair
    (services/secret_expiry_service._block_expired_on re-derives its force the
    same way), so a stale or tenant-scoped value can never open the redaction
    gate while a gov profile is active."""
    if _gov_posture_on(db):
        return True
    try:
        from services import platform_settings
        raw = platform_settings.get_setting(MASKING_SETTING_KEY, db=db)
    except Exception:  # noqa: BLE001
        return True
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in ("0", "false", "no", "off"):
        return False
    return True


def _gov_posture_on(db: Session = None) -> bool:
    """True while a gov/DoD compliance profile is active (FX.117 p4c).

    Imported lazily: services/secret_masking is on the node-run ingest path
    (crud/node_run_cud) and must not grow an import edge that could collide
    with the expiry service's own. A failed lookup is treated as gov (masking
    ON) — the fail-safe direction for this caller is redaction, never
    silence."""
    try:
        from services import secret_expiry_service
        return secret_expiry_service.active_gov_profile(db) is not None
    except Exception:  # noqa: BLE001 — fail toward masking
        return True


def run_secret_values(db: Session, workflow_run_id: Optional[int]) -> List[str]:
    """Plaintext values of the secrets a run references (cached per run id).

    Tenant-scoped: names come from the run's own workflow version graph
    (`graph_json[].data.secret_refs`) and every value resolves against the
    RUN's tenant only — a node can never pull another tenant's secret. Never
    raises: a graph/lookup/decrypt failure yields what was collected (or []),
    degrading masking, never the ingest. The values are never logged here.
    """
    if not workflow_run_id:
        return []
    cached = _RUN_SECRET_CACHE.get(int(workflow_run_id))
    if cached and (time.monotonic() - cached[0]) < RUN_SECRET_CACHE_TTL_S:
        return cached[1]
    values: List[str] = []
    try:
        run = (db.query(models.WorkflowRun)
               .filter(models.WorkflowRun.id == workflow_run_id).first())
        if run and run.tenant_id and run.workflow_version_id:
            ver = (db.query(models.WorkflowVersion)
                   .filter(models.WorkflowVersion.id == run.workflow_version_id).first())
            names = set()
            if ver and isinstance(ver.graph_json, dict):
                for node in (ver.graph_json.get("nodes") or []):
                    for ref in (((node.get("data") or {}).get("secret_refs")) or []):
                        name = ref.get("secret_name") if isinstance(ref, dict) else None
                        if name:
                            names.add(name)
            from crud.secret_cud import get_decrypted_secret_by_name
            for name in sorted(names):
                try:
                    value = get_decrypted_secret_by_name(
                        db, secret_name=name, tenant_id=run.tenant_id)
                except Exception:  # noqa: BLE001 — Vault down: partial masking
                    value = None
                if value:
                    values.append(value)
    except Exception:  # noqa: BLE001 — masking must never break ingest
        return values
    if len(_RUN_SECRET_CACHE) > 256:
        clear_run_secret_cache()
    _RUN_SECRET_CACHE[int(workflow_run_id)] = (time.monotonic(), values)
    return values
