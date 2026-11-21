"""GY.C7 — labelled data never goes to external LLMs.

The data-classification taxonomy lives on ``cogs.data_classification``
(GY.V2a, ``services/data_classification.py``): public < internal <
confidential < restricted < CUI < EAR < ITAR. This module answers one
question for the LLM-egress gate: *does anything this request references
carry a restricted label?* If so, tenant content must NOT leave MOD for an
external (non-local, non-managed) LLM provider — regardless of the tenant's
R-024 opt-in. Local / MOD-hosted bindings are always allowed.

Restricted set (block): ``restricted`` (the legacy highest internal tier) +
the admin-gated federal/export tiers ``CUI``/``EAR``/``ITAR`` (GY.V2b).
An UNRECOGNISED stored label fails CLOSED (treated as restricted) — a
corrupt label must not become an egress loophole, mirroring
``data_classification.requires_us_person``.

The whole block is governed by the platform setting
``labels.block_external_llm`` (bool, default True, group "labels",
registered in ``services/platform_settings.py``). A settings-store read
ERROR fails CLOSED (block) — the opposite of the egress switches' fail-open
defaults, because a broken policy read on a gov/CUI install must not become
a silent data-leak path.

No new columns: labels are read from the existing
``cogs.data_classification``. Workflows/runs are resolved to their input
cogs (run ``parameters.project_info.cog_ids``; a workflow resolves via its
most recent run) and files resolve via ``files.cog_id`` — all existing
lookup paths.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from utils.paid_import import optional_paid

logger = logging.getLogger(__name__)

# Platform setting governing this block (registered in
# services/platform_settings.py). True = restricted-labelled content is
# blocked from external LLM providers.
BLOCK_SETTING_KEY = "labels.block_external_llm"

# Audit event written at the egress gate when a request is refused by this
# policy (routers/ai_config.py).
BLOCK_AUDIT_EVENT = "LLM_EXTERNAL_BLOCKED_LABEL"

# Canonical levels that block external-LLM egress. GY.V8 adds the
# ``biometric`` (BIPA) handling label: biometric data never goes to
# external LLM/HF/web nodes, and even MOD-hosted processing requires an
# active biometric consent per subject (checked in
# ``_subject_externally_blocked``).
BLOCKED_LEVELS = ("restricted", "CUI", "biometric", "EAR", "ITAR")

# GY.V5 — audit event + refusal markers for the minor / consent-revocation
# half of the gate (same surface as BLOCK_AUDIT_EVENT).
MINOR_AUDIT_REASON = "minor_label"
REVOKED_AUDIT_REASON = "consent_revoked"

# GY.V8 — refusal marker for the biometric-consent half of the gate.
BIOMETRIC_CONSENT_REASON = "biometric_consent_required"


def _setting_enabled(db) -> bool:
    """Read ``labels.block_external_llm`` fail-CLOSED: any read error
    blocks (True). The setting defaults to True when healthy."""
    try:
        from services import platform_settings as psr
        return psr.get_setting(BLOCK_SETTING_KEY, db=db) is True
    except Exception as e:  # noqa: BLE001 — fail closed, never raise
        logger.warning("GY.C7: read of %s failed, FAILING CLOSED (block): %s",
                       BLOCK_SETTING_KEY, e)
        return True


def worst_restricted_level(levels: Iterable[Optional[str]]) -> Optional[str]:
    """Highest blocked level among the given labels, else None.

    Returns the canonical level (e.g. ``'ITAR'``) of the worst
    restricted-labelled value. Any UNRECOGNISED non-empty label fails
    closed and is returned as ``'UNKNOWN'`` immediately (a corrupt label
    must never downgrade to allowed).
    """
    from utils.paid_import import optional_paid
    dc = optional_paid("services.data_classification")
    if dc is None:
        # 1788 CE carve: the taxonomy module is paid, but THIS gate ships in CE
        # and must stay fail-closed. Compare the stored value against BLOCKED_LEVELS
        # directly; any other non-empty label is unrecognised -> blocked (the same
        # fail-closed stance as data_classification.requires_us_person).
        # The free-text tiers ship in the column (its default is 'internal'), so
        # they are RECOGNISED-and-allowed here; only a genuinely unrecognised
        # value fails closed as 'UNKNOWN'.
        _allowed = {"public", "internal", "confidential"}
        _blocked = {b.lower(): b for b in BLOCKED_LEVELS}
        for lvl in levels:
            if lvl in (None, ""):
                continue
            _n = str(lvl).strip().lower()
            if _n in _allowed:
                continue
            if _n in _blocked:
                return _blocked[_n]  # canonical spelling, as the paid path does
            return "UNKNOWN"
        return None
    worst: Optional[str] = None
    worst_rank = -1
    for lvl in levels:
        if lvl in (None, ""):
            continue
        try:
            canonical = dc.normalize(lvl)
        except dc.UnknownClassificationError:
            logger.warning("GY.C7: unrecognised data classification %r "
                           "treated as restricted (fail closed)", lvl)
            return "UNKNOWN"
        if canonical in BLOCKED_LEVELS:
            r = dc.rank(canonical)
            if r > worst_rank:
                worst, worst_rank = canonical, r
    return worst


def _cog_levels(db, cog_ids, tenant_id) -> list:
    from models import Cog
    ids = sorted({int(c) for c in cog_ids if isinstance(c, int)
                  and not isinstance(c, bool)})
    if not ids:
        return []
    q = db.query(Cog.data_classification).filter(Cog.id.in_(ids))
    if tenant_id is not None:
        q = q.filter(Cog.tenant_id == tenant_id)
    return [row[0] for row in q.all()]


def _biometric_consent_refusal(
    db, tenant_id, subject_user_ids,
) -> Optional[str]:
    """GY.V8 — refusal reason when the request references BIOMETRIC
    (BIPA) data and the subjects cannot be verified as holding an active
    biometric consent (missing, revoked, or NO subject resolvable — all
    fail CLOSED). Returns None only when every resolved subject has an
    active biometric consent. (External LLM egress is refused for the
    biometric label regardless — this is the CONSENT half of the
    rule, surfaced with a more specific message.)"""
    try:
        from services import consent as _consent
        uids = sorted({int(u) for u in subject_user_ids
                       if u is not None and int(u) >= 0})
        if not uids:
            return ("This data is labelled BIOMETRIC (BIPA, 740 ILCS 14). "
                    "External node / LLM processing is blocked, and no "
                    "verifiable biometric-consent subject is attached to "
                    "this request — consent could not be checked, so it "
                    f"is treated as absent ({BLOCK_SETTING_KEY}).")
        missing = [uid for uid in uids
                   if not _consent.has_active_biometric_consent(
                       db, tenant_id=tenant_id, subject_user_id=uid)]
        if missing:
            return ("This data is labelled BIOMETRIC (BIPA, 740 ILCS 14). "
                    "Processing requires an ACTIVE biometric consent per "
                    "subject (written release with purpose, retention "
                    "period and destruction schedule); none is on file "
                    "for the resolved subject(s) — external node / LLM "
                    f"processing is blocked ({BLOCK_SETTING_KEY}).")
        return None
    except Exception as e:  # noqa: BLE001 — fail closed, never raise
        logger.warning("GY.V8: biometric consent lookup failed, "
                       "FAILING CLOSED (block): %s", e)
        return ("Biometric (BIPA) consent check failed during lookup — "
                "external node egress blocked to be safe.")


def _subject_externally_blocked(
    db, tenant_id, subject_user_ids,
) -> Optional[str]:
    """GY.V5 — refusal reason when any resolved SUBJECT user carries the
    ``minor`` label or has a consent record in REVOKED state (latest
    record wins), else None. Fail closed on lookup errors."""
    uids = sorted({int(u) for u in subject_user_ids
                   if u is not None and int(u) >= 0})
    if not uids:
        return None
    try:
        from models import User
        minors = (db.query(User.id)
                  .filter(User.id.in_(uids))
                  .filter(User.is_minor.is_(True))
                  .all())
        if minors:
            return ("This data is labelled MINOR (COPPA, under-13 subject). "
                    "Minor-labelled data is blocked from external node / LLM "
                    "processing. Route this request to a MOD-hosted "
                    "(local/stack) node, or clear the minor label via the "
                    "admin consent surface after verification. "
                    f"({BLOCK_SETTING_KEY})")
        from services import consent as _consent
        for uid in uids:
            if _consent.subject_consent_revoked(db, tenant_id=tenant_id,
                                                subject_user_id=uid):
                return ("Consent has been REVOKED for this subject; all "
                        "consent-required processing on external nodes is "
                        "blocked and the subject's erasure has been queued "
                        f"({BLOCK_SETTING_KEY}).")
        return None
    except Exception as e:  # noqa: BLE001 — fail closed, never raise
        logger.warning("GY.V5: subject/consent lookup failed, "
                       "FAILING CLOSED (block): %s", e)
        return ("Consent/minor policy check failed during lookup — external "
                "node egress blocked to be safe.")


def external_llm_blocked_for(
    db,
    *,
    tenant_id,
    workflow_id: Optional[int] = None,
    run_id: Optional[int] = None,
    file_ids: Iterable = (),
) -> Optional[str]:
    """Refusal reason when any referenced asset carries a restricted label,
    else None.

    Resolves the referenced objects to cogs (the single owner of
    ``data_classification``):
      * ``file_ids``   — ``files`` rows (by id) -> their ``cog_id``
      * ``run_id``     — ``workflow_runs.parameters['project_info']['cog_ids']``
      * ``workflow_id`` — the workflow's most recent run, same lookup

    Returns a human-readable refusal string naming the worst restricted
    level when blocked. Fail-CLOSED on lookup errors (returns a refusal
    string rather than raising) — a broken lookup must not open an egress
    path. Returns None (allowed) when the setting is off or nothing
    referenced is restricted.
    """
    if not _setting_enabled(db):
        return None
    try:
        cog_ids = []
        subject_user_ids = []
        if file_ids:
            from models import File
            ids = [str(f) for f in file_ids if f is not None]
            if ids:
                q = db.query(File.cog_id, File.user_id).filter(
                    File.id.in_(ids))
                if tenant_id is not None:
                    q = q.filter(File.tenant_id == tenant_id)
                for cog_id, user_id in q.all():
                    if cog_id is not None:
                        cog_ids.append(cog_id)
                    if user_id is not None:
                        subject_user_ids.append(user_id)
        if run_id is not None:
            from models import WorkflowRun
            params = (db.query(WorkflowRun.parameters,
                               WorkflowRun.triggered_by_user_id)
                      .filter(WorkflowRun.id == run_id,
                              WorkflowRun.tenant_id == tenant_id)
                      .first())
            if params is not None:
                if params[1] is not None:
                    subject_user_ids.append(params[1])
                if isinstance(params[0], dict):
                    pi = params[0].get("project_info") or {}
                    cog_ids.extend(pi.get("cog_ids") or [])
        if workflow_id is not None:
            from models import WorkflowRun
            params = (db.query(WorkflowRun.parameters,
                               WorkflowRun.triggered_by_user_id)
                      .filter(WorkflowRun.workflow_id == workflow_id,
                              WorkflowRun.tenant_id == tenant_id)
                      .order_by(WorkflowRun.id.desc())
                      .first())
            if params is not None:
                if params[1] is not None:
                    subject_user_ids.append(params[1])
                if isinstance(params[0], dict):
                    pi = params[0].get("project_info") or {}
                    cog_ids.extend(pi.get("cog_ids") or [])
        cog_ids = [c for c in cog_ids if c is not None]
        levels = _cog_levels(db, cog_ids, tenant_id)
        worst = worst_restricted_level(levels)
        # GY.V8 — biometric is a HANDLING label: it blocks external
        # egress even when mixed with a higher-severity tier, and its
        # consent half is checked per subject below (independent of
        # which tier is "worst").
        has_biometric = any((lvl or "").lower() == "biometric"
                            for lvl in levels)
    except Exception as e:  # noqa: BLE001 — fail closed, never raise
        logger.warning("GY.C7: label lookup failed, FAILING CLOSED (block): %s",
                       e)
        return ("Restricted-label policy check failed during lookup — "
                "external LLM egress blocked to be safe.")
    # GY.V5 — the minor data label + consent revocation: a minor-labelled
    # subject (or one whose consent has been revoked) blocks ALL
    # consent-required processing on external nodes, through this same C7
    # gate. Fail closed: a lookup error here returns a refusal.
    subject_reason = _subject_externally_blocked(db, tenant_id,
                                                 subject_user_ids)
    if subject_reason is not None:
        return subject_reason
    # GY.V8 — biometric (BIPA): processing of biometric-labelled data
    # requires an ACTIVE biometric consent per resolved subject. A
    # missing/revoked consent (or no subject at all to verify) blocks.
    # WITH a valid consent the biometric label still blocks EXTERNAL
    # egress (see the refusal below — biometric data never leaves MOD);
    # the consent check is what gates consent-required processing as
    # such.
    if has_biometric:
        bio_reason = _biometric_consent_refusal(db, tenant_id,
                                                subject_user_ids)
        if bio_reason is not None:
            return bio_reason
    if worst is None:
        return None
    return (f"Tenant data is labelled {worst} and is blocked from external "
            f"LLM providers ({BLOCK_SETTING_KEY}). Route this request to a "
            f"MOD-hosted (local/stack) LLM endpoint, or lower the "
            f"classification of the labelled asset(s). No fallback model "
            f"was used.")


# ---------------------------------------------------------------------------
# GY.C7 — node-dispatch side of label handling: external-egress block +
# worker pool/region policy.
#
# The LLM gate above (external_llm_blocked_for) is the last line at the
# provider boundary. These two helpers are the DISPATCH line: they decide,
# before a task row is written, (1) whether a node that declares INTERNET
# egress may run on labelled inputs, and (2) which worker pools/regions may
# claim a labelled task. Both are driven by the input cogs'
# ``data_classification`` and read their policy from platform settings, so
# they change nothing for unlabelled (public/internal/confidential) runs.
# ---------------------------------------------------------------------------

NO_EGRESS_SETTING_KEY = "labels.no_external_egress_labels"
# Registry default (must match the register() default in
# services/platform_settings.py); used only as the fallback when a
# settings-store read errors.
NO_EGRESS_DEFAULT = ("cui", "itar", "ear")
NO_EGRESS_AUDIT_EVENT = "LABEL_EXTERNAL_EGRESS_BLOCKED"

POOLS_SETTING_KEY = "labels.allowed_worker_pools"


def node_is_external(allowed_egress) -> bool:
    """True when a node declaration carries any INTERNET egress — an external
    LLM API, HF download, or web fetch.

    ``allowed_egress`` is the node/task egress declaration (a list of host
    strings and/or ``{host, ports, protocol}`` dicts, see
    ``services.egress_allowlist.compile_allowlist``). Each compiled entry
    has a ``kind`` — ``internet`` (leaves MOD) or ``tailnet`` (MOD's own
    mesh, egressed via the tsegress sidecar). Only ``internet`` entries make
    the node EXTERNAL; a tailnet-only or empty declaration is not external.
    A declaration that compiles to nothing is treated as external (fail
    CLOSED) so a corrupt entry cannot open an egress path.
    """
    if not allowed_egress:
        return False
    try:
        from services.egress_allowlist import compile_allowlist, KIND_INTERNET
        entries = compile_allowlist(allowed_egress)
    except Exception:
        # Non-empty but uncompilable: fail CLOSED — do not assume internal.
        return True
    if not entries:
        return False
    return any(getattr(e, "kind", "") == KIND_INTERNET for e in entries)


def _cog_classifications(db, cog_ids, tenant_id) -> list:
    """Lower-cased data_classification values for the given cog ids.

    Returns an empty list (never raises) on a lookup error — callers that
    want fail-closed behaviour must handle the empty-vs-error distinction
    themselves. Int/bool-normalised, tenant-scoped like the LLM gate."""
    ids = sorted({int(c) for c in (cog_ids or [])
                  if isinstance(c, int) and not isinstance(c, bool)})
    if not ids:
        return []
    from models import Cog
    q = db.query(Cog.data_classification).filter(Cog.id.in_(ids))
    if tenant_id is not None:
        q = q.filter(Cog.tenant_id == tenant_id)
    return [str(l).strip().lower() for l in (row[0] for row in q.all()) if l]


def no_external_egress_label_for(db, cog_ids,
                                 tenant_id: Optional[int] = None) -> Optional[str]:
    """The matching no-external-egress label among a task's input cogs, or None.

    Reads ``labels.no_external_egress_labels`` (default
    ``['cui','itar','ear','pii','pre_release']``) and returns the highest-severity
    cogs' classification that is a member of that list (lower-cased), else
    None when nothing matches. None means "do not block" — that is the case
    for every unlabelled (public/internal/confidential) run, so the default
    is a no-op for ordinary work.

    Fails OPEN on a cogs-lookup error (returns None) — matching the
    GY.V2b export-control stamp precedent, which relies on the independent
    claim-side guard + the labels.block_external_llm provider gate as
    backstops; a transient DB error must never wedge dispatch. A settings
    read error falls back to the registry default list (fail toward the
    block, never toward an empty list).

    GY.C7 s3 (tenant custom labels): a customer-defined label never appears
    in ``cogs.data_classification`` — it lives on ``data_markings``. So the
    gate additionally unions in every custom code whose tenant rule carries
    ``external_node_block=True`` and matches those against the tenant's
    LIVE marks on these cogs (:func:`custom_mark_codes`). A custom code is
    returned verbatim (case as stored) so the reason string names the
    customer's own label. Still fail-open on any lookup error.
    """
    try:
        from services import platform_settings as psr
        blocked = psr.get_setting(NO_EGRESS_SETTING_KEY, db=db)
    except Exception as e:  # noqa: BLE001
        logger.warning("GY.C7: read of %s failed, using default list: %s",
                       NO_EGRESS_SETTING_KEY, e)
        blocked = list(NO_EGRESS_DEFAULT)
    blocked_norm = {str(x).strip().lower() for x in (blocked or []) if str(x).strip()}
    # GY.C7 s3 — the tenant's custom codes that block external nodes.
    custom_blocked = {
        str(k).strip(): v for k, v in _custom_rules_map(db, tenant_id).items()
        if str(k).strip() and isinstance(v, dict) and bool(v.get("external_node_block"))
    }
    if not blocked_norm and not custom_blocked:
        return None
    try:
        levels = _cog_classifications(db, cog_ids, tenant_id)
    except Exception as e:  # noqa: BLE001 — fail open on lookup error
        logger.warning("GY.C7: no-external-egress cog lookup failed "
                       "(task proceeds; V2b claim guard + LLM gate remain): %s", e)
        return None
    matched = [l for l in levels if l in blocked_norm]
    if custom_blocked:
        try:
            marks = custom_mark_codes(db, cog_ids, tenant_id)
        except Exception as e:  # noqa: BLE001 — fail open
            logger.warning("GY.C7 s3: custom-mark lookup failed "
                           "(only platform labels considered): %s", e)
            marks = []
        matched += [c for c in marks if c in custom_blocked]
    if not matched:
        return None
    # Highest-severity match (by taxonomy rank when known) for a stable
    # reason string; any match blocks.
    try:
        from services import data_classification as _dc
        def _rank(l):
            try:
                return _dc.rank(l)
            except Exception:
                return -1
        return max(matched, key=lambda l: (_rank(l), l))
    except Exception:  # noqa: BLE001
        return matched[0]


def restricted_pool_list(db, cog_ids,
                         tenant_id: Optional[int] = None,
                         rules_map: Optional[dict] = None) -> Optional[list]:
    """The worker pools a task's inputs restrict it to, or None.

    Reads ``labels.allowed_worker_pools`` (a map of label -> list of pool /
    region / worker names; default ``{}`` = no restriction). For the cogs'
    classifications, takes the INTERSECTION of the pool lists for every
    label that is a key in the map (conflicting restrictions -> empty -> the
    task waits rather than routing somewhere unsafe). Returns:
      * ``None``  -> no restriction (no restricted label present, empty map,
        or no cogs) — the task is claimable by any worker;
      * a list (possibly empty) -> restricted to those pool names. An empty
        list means no pool currently matches (the task waits, fail-wait).

    Fails OPEN on any error (returns None): a policy-lookup hiccup must not
    strand or misroute ordinary tasks; the pool restriction is a placement
    preference layer, not the primary data-protection gate.

    GY.C7 s3: ``rules_map`` (when given) REPLACES the platform settings read
    with a caller-supplied label -> pool-names map — the tenant-scoped
    ``labels.custom_label_rules`` read (``custom_label_rules``) is done once
    by the caller and merged with the platform map, so the tenant's own
    label rules ride the exact same intersection logic and claim guard. The
    body below is unchanged.
    """
    if rules_map is not None:
        pools = rules_map
    else:
        try:
            from services import platform_settings as psr
            pools = psr.get_setting(POOLS_SETTING_KEY, db=db)
        except Exception as e:  # noqa: BLE001
            logger.warning("GY.C7: read of %s failed (no pool restriction): %s",
                           POOLS_SETTING_KEY, e)
            return None
    if not isinstance(pools, dict) or not pools:
        return None
    norm_map = {}
    for k, v in pools.items():
        # GY.C7 s3: a rules_map may be the raw labels.custom_label_rules map,
        # whose entries are rule DICTS — take the 'pools' list from them.
        if isinstance(v, dict):
            v = v.get("pools")
        if isinstance(v, (list, tuple)):
            names = [str(x).strip().lower() for x in v if str(x).strip()]
            norm_map[str(k).strip().lower()] = names
    if not norm_map:
        return None
    try:
        levels = _cog_classifications(db, cog_ids, tenant_id)
    except Exception as e:  # noqa: BLE001 — fail open
        logger.warning("GY.C7: pool-restriction cog lookup failed "
                       "(no pool restriction): %s", e)
        return None
    restricted = [norm_map[l] for l in levels if l in norm_map]
    if not restricted:
        return None
    allowed = set(restricted[0])
    for lst in restricted[1:]:
        allowed &= set(lst)
    return sorted(allowed)


def worker_pool_candidates(worker) -> list:
    """The pool/region names a worker claims as its own (deduped, lower-case):
    its ``worker_name``, its hardware tags (``hardware_specs['hardware_tags']``,
    e.g. ``region:us``), and — for a ``region:<zone>`` tag — the bare zone
    ``us``. Used to match ``labels.allowed_worker_pools`` at claim time.

    Reads the RAW hardware tags directly (never request data) so the
    candidates are exactly what the operator set, independent of residency
    zone-table availability: an unknown/missing zone simply adds nothing
    beyond the explicit tags."""
    names = []
    wn = getattr(worker, "worker_name", None)
    if wn:
        names.append(str(wn).strip().lower())
    specs = getattr(worker, "hardware_specs", None)
    tags = specs.get("hardware_tags") if isinstance(specs, dict) else None
    for tag in tags or []:
        t = str(tag).strip().lower()
        if not t or t in names:
            continue
        names.append(t)
        # 'region:us' -> also expose bare 'us' as a pool candidate.
        if t.startswith("region:"):
            zone = t[len("region:"):].strip()
            if zone and zone not in names:
                names.append(zone)
    return names


def block_external_egress(db, *, run_id, node_run_id, node_id, tenant_id,
                          label) -> None:
    """Fail a node run + its workflow run because an EXTERNAL-egress node was
    about to process labelled inputs, and write the refusal audit event.

    Best-effort and idempotent — NEVER raises (a policy-record hiccup must
    not mask the dispatch refusal). Mirrors the GY.C7 dispatch-loop fail
    pattern: the node run + run go to FAILED with a clear, actionable reason,
    and an append-only ``LABEL_EXTERNAL_EGRESS_BLOCKED`` audit row records
    actor/tenant/node/label for the compliance trail.
    """
    reason = (
        f"External-egress node blocked: the run's inputs carry the "
        f"'{label}' data classification, which is on the "
        f"{NO_EGRESS_SETTING_KEY} no-external-egress list. This node "
        f"declares internet egress (external LLM / HF download / web "
        f"fetch) and is refused before any task is dispatched. Lower the "
        f"classification or use a non-external (MOD-hosted/tailnet) node."
    )
    try:
        from models import NodeRun, WorkflowRun, TaskStatus
        import datetime as _dt
        nr = None
        if node_run_id is not None:
            nr = db.query(NodeRun).filter(
                NodeRun.id == node_run_id).first()
            if nr and nr.status not in (TaskStatus.SUCCESS,
                                        TaskStatus.COMPLETED,
                                        TaskStatus.FAILED):
                nr.status = TaskStatus.FAILED
                nr.error_message = reason
                if not nr.completed_at:
                    nr.completed_at = _dt.datetime.now(_dt.timezone.utc)
        if run_id is not None:
            run = db.query(WorkflowRun).filter(
                WorkflowRun.id == run_id).first()
            if run and run.status not in (TaskStatus.COMPLETED,
                                          TaskStatus.FAILED):
                run.status = TaskStatus.FAILED
                run.error_message = (
                    f"Run aborted: node '{node_id}' was refused "
                    f"(external-egress block, label '{label}').")
                if not run.completed_at:
                    run.completed_at = _dt.datetime.now(_dt.timezone.utc)
        db.commit()
    except Exception:  # noqa: BLE001 — state update best-effort
        logger.exception("GY.C7: failed to mark external-egress block on "
                         "run %s / node run %s", run_id, node_run_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    try:
        if optional_paid("services.data_classification") is not None:
            from services import data_classification as _dc
            _dc.record_audit(
                db,
                tenant_id=tenant_id,
                user=None,  # system path — principal resolved internally
                event_name=NO_EGRESS_AUDIT_EVENT,
                details={
                    "workflow_run_id": run_id,
                    "node_run_id": getattr(node_run_id, "id", node_run_id),
                    "node_id": node_id,
                    "label": label,
                    "setting": NO_EGRESS_SETTING_KEY,
                },
            )
    except Exception:  # noqa: BLE001
        logger.exception("GY.C7: failed to write %s audit row",
                         NO_EGRESS_AUDIT_EVENT)


# ---------------------------------------------------------------------------
# GY.C7 s2 — REGION allowlist on top of the pool policy.
#
# ``labels.allowed_regions`` maps a data-classification label -> the list of
# worker REGIONS ('us' / 'eu', the zone half of a worker's 'region:<zone>'
# hardware tag) that may process it. It is the same store, the same shape and
# the same posture as ``labels.allowed_worker_pools`` (which matches whole
# pool/region TAGS and worker names); the region key is the narrower,
# geography-only form so an operator can say "ITAR never leaves the US box"
# without naming boxes. Enforcement rides the SAME claim choke point as the
# pool guard (routers/worker_tasks._label_region_guard_sql): a labelled task
# whose region restriction excludes this worker is WITHHELD from it (fail-wait
# — the task stays PENDING and an allowed-region worker picks it up), and the
# unrunnable sweep (services/scheduler.py) surfaces the reason string so the
# Dashboard banner says WHY instead of hanging silently. {} default = no
# restriction = byte-identical behaviour; unlabelled runs never match.
# ---------------------------------------------------------------------------

REGIONS_SETTING_KEY = "labels.allowed_regions"
REGION_TAG_PREFIX = "region:"

# GY.C7 s2 registers its key HERE (services/platform_settings.py is off-
# limits to this lane; the pool keys landed there in 72b68368e). register()
# is a no-op on re-registration, so a later registry-side entry cannot
# clobber this spec. The import is safe: nothing platform_settings imports
# (models/audit/httpx) imports label_policy.
import services.platform_settings as _psr_reg  # noqa: E402
_psr_reg.register(
    REGIONS_SETTING_KEY,
    dict,
    {},
    group="labels",
    description="GY.C7 s2 — per-label worker REGION allowlist. Maps a "
                "data-classification label to a list of worker regions "
                "(the zone half of a worker's 'region:<zone>' hardware "
                "tag: 'us'/'eu'; a 'region:us' value is accepted too) that "
                "may process it. {} (default) = no restriction — every "
                "worker can claim any labelled task. When a label IS "
                "restricted, a labelled task is withheld from every "
                "worker outside the allowed regions at claim time "
                "(routers/worker_tasks._label_region_guard_sql; fail-"
                "wait: no matching worker -> the task simply waits, it "
                "is never routed somewhere unsafe). Unlabelled runs are "
                "byte-identical. Read via "
                "utils/label_policy.restricted_region_list.",
)


def restricted_region_list(db, cog_ids,
                           tenant_id: Optional[int] = None,
                           rules_map: Optional[dict] = None) -> Optional[list]:
    """The worker regions a task's inputs restrict it to, or None.

    Mirrors :func:`restricted_pool_list` for ``labels.allowed_regions``: for
    every input-cog classification that is a key in the map, take the
    INTERSECTION of the region lists (conflicting restrictions -> empty ->
    the task waits rather than routing somewhere unsafe). Region names are
    normalised (lower-cased, a ``region:`` prefix stripped) so ``"US"`` and
    ``"region:us"`` in the settings both mean zone ``us``.

    Returns ``None`` (no restriction) or a list (possibly empty). Fails OPEN
    on any error — the same placement-layer posture as the pool policy: the
    primary data-protection gates (external-egress block, export control)
    stay authoritative.

    GY.C7 s3: ``rules_map`` (when given) REPLACES the platform settings read
    with a caller-supplied label -> region-names map (the tenant-scoped
    ``labels.custom_label_rules`` entries are merged into it by the caller),
    so a tenant's own label rules ride the exact same normalisation,
    intersection and claim guard as the platform map. Body unchanged.
    """
    if rules_map is not None:
        regions = rules_map
    else:
        try:
            from services import platform_settings as psr
            regions = psr.get_setting(REGIONS_SETTING_KEY, db=db)
        except Exception as e:  # noqa: BLE001
            logger.warning("GY.C7 s2: read of %s failed (no region restriction): %s",
                           REGIONS_SETTING_KEY, e)
            return None
    if not isinstance(regions, dict) or not regions:
        return None

    def _norm_zone(value) -> Optional[str]:
        z = str(value).strip().lower()
        if z.startswith(REGION_TAG_PREFIX):
            z = z[len(REGION_TAG_PREFIX):].strip()
        return z or None

    norm_map: dict = {}
    for k, v in regions.items():
        # GY.C7 s3: a rules_map may be the raw labels.custom_label_rules map,
        # whose entries are rule DICTS — take the 'regions' list from them.
        if isinstance(v, dict):
            v = v.get("regions")
        if not isinstance(v, (list, tuple)):
            continue
        zones = sorted({_norm_zone(x) for x in v if _norm_zone(x)})
        key = _norm_zone(k)
        if key:
            norm_map[key] = zones
    if not norm_map:
        return None
    try:
        levels = _cog_classifications(db, cog_ids, tenant_id)
    except Exception as e:  # noqa: BLE001 — fail open
        logger.warning("GY.C7 s2: region-restriction cog lookup failed "
                       "(no region restriction): %s", e)
        return None
    restricted = [norm_map[l] for l in levels if l in norm_map]
    if not restricted:
        return None
    allowed = set(restricted[0])
    for lst in restricted[1:]:
        allowed &= set(lst)
    return sorted(allowed)


def worker_region_candidates(worker) -> list:
    """The region zones a worker claims as its own (deduped, lower-case).

    Reads ONLY the worker's own ``hardware_specs['hardware_tags']`` — a
    ``region:<zone>`` tag contributes both the full tag and the bare zone
    (``region:us`` -> ``region:us`` + ``us``) — never request data, so the
    candidates are exactly what the operator set. A worker with no region
    tag claims NO region: it matches an empty candidate list and is withheld
    from every region-restricted task (fail-closed, same posture as the pool
    guard's no-provable-identity case).
    """
    names: list = []
    specs = getattr(worker, "hardware_specs", None)
    tags = specs.get("hardware_tags") if isinstance(specs, dict) else None
    for tag in tags or []:
        t = str(tag).strip().lower()
        if not t or not t.startswith(REGION_TAG_PREFIX):
            continue
        if t not in names:
            names.append(t)
        zone = t[len(REGION_TAG_PREFIX):].strip()
        if zone and zone not in names:
            names.append(zone)
    return names


# ---------------------------------------------------------------------------
# GY.C7 s3 — CUSTOM (tenant-defined) label rules
# ---------------------------------------------------------------------------
#
# ``labels.custom_label_rules`` is the TENANT-scoped sibling of the three
# platform-wide label maps above: a customer's own MarkingLabelDef (CUI-
# style codes they invent, e.g. ``ACME-RED``) carries its pool / region /
# watermark / external-node rules in ONE dict keyed by the label code, so
# the tenant never has to abuse the platform keys (which are global and
# would leak their label names to every other tenant).
#
# Shape: ``{"ACME-RED": {"pools": ["dell"], "regions": ["us"],
#                        "watermark": true, "external_node_block": true}}``.
# Resolution is the settings store's own tenant-first lookup
# (``get_setting(..., tenant_id=)``), so tenant_2 never sees tenant_1's
# rules — the same isolation ``list_labels`` gives the catalogue.
#
# Enforcement rides the SAME choke points as the platform maps: the pool +
# region stamps/claim guards (which now accept a ``rules_map`` so the
# tenant-scoped read is done once by the caller) and the external-egress
# gate, which additionally matches a tenant's LIVE DataMarking codes
# (``custom_mark_codes``) because a custom code is never stored in
# ``cogs.data_classification`` — the mark lives on ``data_markings``.
#
# Fail-open posture is identical to the placement layer above: a broken
# read must never wedge dispatch; the primary gates (external-egress block,
# export control, claim guards) stay authoritative.
CUSTOM_RULES_SETTING_KEY = "labels.custom_label_rules"

import services.platform_settings as _psr_reg_s3  # noqa: E402
_psr_reg_s3.register(
    CUSTOM_RULES_SETTING_KEY,
    dict,
    {},
    group="labels",
    description="GY.C7 s3 — tenant-defined data-marking label RULES "
                "(tenant-scoped). Maps a tenant's own MarkingLabelDef code "
                "to its placement/egress rules: 'pools' (worker pool/region "
                "/worker names allowed to process it, same matching as "
                "labels.allowed_worker_pools), 'regions' (worker region "
                "zones, same matching as labels.allowed_regions), "
                "'watermark' (reserved for the s3c propagation step) and "
                "'external_node_block' (True = inputs carrying this label "
                "may never be processed by an external-egress node). {} "
                "(default) = no tenant rules at all, byte-identical "
                "behaviour. Written by services/data_markings.create_tenant_"
                "label/update_tenant_label_rules via set_setting(tenant_id=)"
                "; read by utils/label_policy.custom_label_rules, "
                "restricted_pool_list/restricted_region_list (rules_map=) "
                "and no_external_egress_label_for.",
)


def _custom_rules_map(db, tenant_id: Optional[int]) -> dict:
    """The tenant's whole ``labels.custom_label_rules`` map ({} on miss/error).

    One read shared by :func:`custom_label_rules` and the egress gate below;
    tenant-scoped, so another tenant's rules are never visible."""
    if tenant_id is None:
        return {}
    try:
        from services import platform_settings as psr
        rules = psr.get_setting(CUSTOM_RULES_SETTING_KEY, db=db,
                                tenant_id=tenant_id)
    except Exception as e:  # noqa: BLE001 — fail open
        logger.warning("GY.C7 s3: read of %s for tenant %s failed "
                       "(no custom rules applied): %s",
                       CUSTOM_RULES_SETTING_KEY, tenant_id, e)
        return {}
    return rules if isinstance(rules, dict) else {}


def _rule_lookup(rules: dict, code: str) -> Optional[dict]:
    """One entry of a custom-rules map by code (exact, then lower-case)."""
    c = str(code).strip()
    entry = rules.get(c)
    if entry is None:
        entry = rules.get(c.lower())
    return entry if isinstance(entry, dict) else None


def custom_label_rules(db, tenant_id: Optional[int],
                       code: str) -> Optional[dict]:
    """The tenant's own rule dict for one custom label code, or None.

    Reads the tenant-scoped ``labels.custom_label_rules`` row (the settings
    store resolves tenant > platform > default, so another tenant's rules
    are never visible). Returns the rule dict as stored, or ``None`` when
    the key is unset/empty, the code is not a key, or the entry is not a
    dict. Fails OPEN (None) on any read error — a broken policy read must
    not wedge dispatch; the independent provider/claim gates remain."""
    if tenant_id is None or not code:
        return None
    return _rule_lookup(_custom_rules_map(db, tenant_id), code)


def custom_mark_codes(db, cog_ids,
                      tenant_id: Optional[int] = None) -> list:
    """The tenant's LIVE custom data-marking codes on these input cogs.

    ``data_markings`` (GSEC FX.79) is where a customer's own label codes
    live — a custom code is never written into ``cogs.data_classification``,
    so the classification reads above cannot see it. This mirrors the
    ``services/data_markings.blocking_labels`` query (live rows only,
    ``deleted_at IS NULL``, scoped to the acting tenant so a foreign
    tenant's marks never match) narrowed to the task's input cogs:
    ``resource_type == 'asset'`` with ``resource_id`` the cog id.

    Returns a sorted, de-duplicated list of codes (empty when nothing is
    marked). Fails OPEN ([]) on a lookup error — same posture as every
    other placement-layer read here."""
    if tenant_id is None:
        return []
    ids = [int(i) for i in cog_ids if str(i).strip().lstrip("-").isdigit()]
    if not ids:
        return []
    try:
        from models import DataMarking
        rows = (
            db.query(DataMarking.label)
            .filter(
                DataMarking.tenant_id == tenant_id,
                DataMarking.resource_type == "asset",
                DataMarking.resource_id.in_([str(i) for i in ids]),
                DataMarking.deleted_at.is_(None),
            )
            .all()
        )
    except Exception as e:  # noqa: BLE001 — fail open
        logger.warning("GY.C7 s3: custom-mark cog lookup failed "
                       "(no custom codes considered): %s", e)
        return []
    return sorted({str(r[0]).strip() for r in rows if str(r[0]).strip()})
