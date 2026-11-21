"""GO.8 on-change half (23.7.s1) — regression gate for variant / recipe /
base-image changes.

The weekly half (``corpus_gate.enqueue_weekly`` + ``evaluate_weekly``) re-runs
the whole fast subset on a schedule. This module is the ON-CHANGE half: when
something the variant pipeline runs on actually lands — a variant-strategy
change, an install-recipe change, or a base-image rebuild — it re-verifies
ONLY the variants that change can affect, through the EXISTING
``queue.enqueue_pending`` path (no harness duplication), and compares each
fresh verdict against the incumbent one. A regression (an incumbent-verified
variant that now fails/drifts, or a score drop beyond
``node_build.variant_change_gate_score_drop``) marks the change regressed:
one ``VARIANT_CHANGE_REGRESSED`` audit event and ``promotion_blocked`` —
the caller must not promote the change while it is true.

Three change kinds, three blast radii:

  - **variant change** (``variant_strategy.py`` / anything under
    ``variant_test/`` — the same trigger set as ``corpus_gate.TRIGGER_GLOBS``
    for the variant half): the variant matrix itself moved, so every
    already-converted variant of every converted Node is affected.
  - **recipe change** (``(library_name, accelerator)`` pairs — the identity
    of a ``LibraryInstallRecipe`` row): only variants on that accelerator of
    Nodes that actually reference the library are affected. A cuda-recipe
    change never re-verifies a cpu variant.
  - **base-image change** (base image tags, e.g. ``python-cuda-12.4``): only
    converted variants whose built image ref derives from that tag are
    affected. This is the hook site for "base image changed" — there is no
    separate base-image-change event in the API;
    ``utils/base_image_rebuild.enqueue_nightly_base_rebuilds`` +
    ``services/base_image_weekly.run_weekly_rebuild`` rebuild the curated
    tags and hand the resulting tags to this function.

Dark by default: ``node_build.variant_change_gate_enabled`` (bool, default
false) must be on for anything to enqueue or block. Tunables are platform
settings, never env vars.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from services import platform_settings

_LOGGER = logging.getLogger(__name__)

ENABLED_KEY = "node_build.variant_change_gate_enabled"
DROP_KEY = "node_build.variant_change_gate_score_drop"
DEFAULT_SCORE_DROP = 0.05

# Verdicts that mean "this variant is serving trustworthy output".
_GOOD_VERDICTS = frozenset(("verified", "marginal"))
_BAD_VERDICTS = frozenset(("failed", "incompatible", "drift"))
# variant_verification_status labels that mean the same (corpus_harness
# writes these labels via record_verification_status).
_GOOD_STATUSES = frozenset(("verified",))

# 23.7.s2 — the weekly sweep report's audit row. There is no report/digest
# AuditAction member in the enum (same finding as
# services/privileged_elevation_summary.py:76-82 and
# services/support_ticket_report.py:41), so this part reuses
# ADMIN_SYSTEM_MAINTENANCE — the existing label every other weekly platform
# report writes under — and the ``event`` key in audit_metadata is what names
# the row as THIS sweep's report. No enum value, no migration.
WEEKLY_EVENT = "VARIANT_CHANGE_GATE_WEEKLY_REPORT"

# The variant-matrix half of corpus_gate.TRIGGER_GLOBS: a change here moves
# the variant set itself, so every converted variant must be re-verified.
VARIANT_TRIGGER_GLOBS: Tuple[str, ...] = (
    "node_builder/build_dag/variant_strategy.py",
    "node_builder/variant_test/**",
)


def register_settings() -> None:
    """Register the on-change gate's platform settings (idempotent)."""
    platform_settings.register(
        ENABLED_KEY,
        bool,
        False,
        group="node_build",
        description="GO.8 (23.7.s1) on-change regression gate: when true, a "
                    "change to the variant strategy, an install recipe, or a "
                    "base image re-verifies ONLY the affected variants "
                    "through the variant-test queue and compares each fresh "
                    "verdict to the incumbent one. Off by default.",
    )
    platform_settings.register(
        DROP_KEY,
        float,
        DEFAULT_SCORE_DROP,
        group="node_build",
        description="GO.8 (23.7.s1) score-drop tolerance: a fresh sample-test "
                    "score this far below the incumbent best_score is a "
                    "regression even when the verdict label is unchanged.",
    )


def _enabled(db) -> bool:
    try:
        return bool(platform_settings.get_setting(ENABLED_KEY, db=db))
    except Exception:  # not registered yet / DB down -> off
        return False


def _score_drop(db) -> float:
    try:
        value = platform_settings.get_setting(DROP_KEY, db=db)
    except Exception:
        return DEFAULT_SCORE_DROP
    try:
        return float(value)
    except (TypeError, ValueError):
        return DEFAULT_SCORE_DROP


def _norm(path: str) -> str:
    p = str(path or "").strip().replace("\\", "/")
    while p.startswith("./") or p.startswith("/"):
        p = p.lstrip("./")
    return p


def variant_change_landed(changed_paths: Iterable[str]) -> bool:
    """True when a changed path hits the variant-matrix trigger set (the
    variant half of ``corpus_gate.TRIGGER_GLOBS`` — repo-root-relative and
    API-relative paths both match)."""
    from fnmatch import fnmatch
    for p in changed_paths:
        n = _norm(p)
        if not n:
            continue
        candidates = [n] + [n[i + 1:] for i, c in enumerate(n) if c == "/"]
        for pat in VARIANT_TRIGGER_GLOBS:
            for cand in candidates:
                if fnmatch(cand, pat) or fnmatch(cand, pat.rstrip("/**")):
                    return True
    return False


# ---------------------------------------------------------------------------
# Blast radius: which (node, variant) pairs a change can affect
# ---------------------------------------------------------------------------

def _converted_nodes(db) -> List[Any]:
    """Live NodeDefinitions with a populated ``gpu_variants`` map — the
    same selection ``corpus_harness.converted_variant_cells`` uses, but
    keeping the row so the queue gets a real ``node_definition_id``."""
    import models
    rows = (
        db.query(models.NodeDefinition)
        .filter(models.NodeDefinition.deleted_at.is_(None))
        .all()
    )
    return [nd for nd in rows if _node_variants(nd)]


def _node_variants(nd) -> Dict[str, str]:
    gv = nd.gpu_variants if isinstance(nd.gpu_variants, dict) else {}
    return {str(k).strip().lower(): str(v) for k, v in gv.items()
            if str(k).strip().lower() and v}


def _base_image_matches(image_ref: str, tag: str) -> bool:
    """A built variant image ref derives from a curated base tag when the
    tag is the image name or part of it (``.../python-cuda-12.4:latest-cuda``
    for base tag ``python-cuda-12.4``)."""
    tail = str(image_ref or "").split("/")[-1].split(":")[0]
    tag = str(tag or "").strip().lower()
    if not tail or not tag:
        return False
    tail = tail.lower()
    return tail == tag or tag in tail.split("-") or tag in tail


def _libraries_of_node(db, nd) -> List[str]:
    """Libraries a converted Node's build references — read from the BuildSpec
    its verification rows point at (``install_recipes.libraries_referenced_in_spec``
    is the SAME extractor the recipe writer uses, so the two stay consistent).
    Best-effort: a node with no recoverable spec references nothing."""
    import models
    from node_builder.install_recipes import libraries_referenced_in_spec
    rows = (
        db.query(models.NodeVariantVerification)
        .filter(
            models.NodeVariantVerification.node_definition_id == nd.id,
            models.NodeVariantVerification.deleted_at.is_(None),
            models.NodeVariantVerification.build_spec_id.isnot(None),
        )
        .all()
    )
    libs: List[str] = []
    seen = set()
    for r in rows:
        rec = (
            db.query(models.BuildSpecRecord)
            .filter(models.BuildSpecRecord.id == r.build_spec_id)
            .first()
        )
        if rec is None or not isinstance(rec.spec_json, dict):
            continue
        for lib in libraries_referenced_in_spec(rec.spec_json):
            if lib and lib not in seen:
                seen.add(lib)
                libs.append(lib)
    return libs


def affected_variants(
    db,
    *,
    variant_change: bool = False,
    recipes: Iterable[Tuple[str, str]] = (),
    base_images: Iterable[str] = (),
) -> List[Dict[str, Any]]:
    """The (node, variant) pairs a change set can affect.

    ``variant_change`` (the variant matrix moved) → every converted variant.
    ``recipes`` ``[(library_name, accelerator)]`` → only variants on that
    accelerator of Nodes that reference that library. ``base_images`` (curated
    base tags) → only variants whose built image ref derives from the tag.
    Unaffected variants never appear. Keys are deduped across the three kinds.
    """
    targets: Dict[Tuple[int, str], Dict[str, Any]] = {}

    def add(nd: Any, variant: str, reason: str) -> None:
        key = (int(nd.id), str(variant).lower())
        if key in targets:
            return
        gv = _node_variants(nd)
        image_ref = gv.get(variant.lower(), "")
        targets[key] = {
            "node_definition_id": int(nd.id),
            "node_kind": str(nd.node_kind or nd.name or ""),
            "variant": variant.lower(),
            "image_ref": image_ref,
            "reason": reason,
        }

    nodes = _converted_nodes(db)
    if variant_change:
        for nd in nodes:
            for variant in _node_variants(nd):
                add(nd, variant, "variant_strategy")
        return list(targets.values())

    recipe_pairs = {(str(lib).lower(), str(accel).lower())
                    for lib, accel in recipes if lib and accel}
    for lib, accel in sorted(recipe_pairs):
        for nd in nodes:
            libs = [str(x).lower() for x in _libraries_of_node(db, nd)]
            if lib not in libs:
                continue
            for variant in _node_variants(nd):
                if variant == accel:
                    add(nd, variant, f"recipe:{lib}@{accel}")

    tags = [str(t).strip().lower() for t in base_images if str(t).strip()]
    for tag in tags:
        for nd in nodes:
            for variant, image_ref in _node_variants(nd).items():
                if _base_image_matches(image_ref, tag):
                    add(nd, variant, f"base_image:{tag}")

    return list(targets.values())


# ---------------------------------------------------------------------------
# Enqueue: re-verify ONLY the affected variants (existing queue path)
# ---------------------------------------------------------------------------

def enqueue_on_change(
    db,
    *,
    changed_paths: Iterable[str] = (),
    recipes: Iterable[Tuple[str, str]] = (),
    base_images: Iterable[str] = (),
    ttl_days: int = 30,
) -> Dict[str, Any]:
    """Land an on-change gate run: enqueue a pending sample-test for every
    affected (node, variant) through ``queue.enqueue_pending`` (the existing
    worker-heartbeat drain — nothing here builds or dispatches).

    No-op unless ``node_build.variant_change_gate_enabled`` is on. Returns
    ``{enqueued, targets, enabled}``; ``targets`` is the blast radius so the
    caller (and the later evaluation) knows exactly which variants the change
    touched.
    """
    register_settings()
    if not _enabled(db):
        return {"enqueued": 0, "targets": [], "enabled": False}

    from .queue import enqueue_pending
    targets = affected_variants(
        db,
        variant_change=variant_change_landed(changed_paths),
        recipes=recipes,
        base_images=base_images,
    )
    enqueued: List[int] = []
    for t in targets:
        enqueued.append(
            enqueue_pending(
                db,
                build_spec_id=None,
                node_definition_id=t["node_definition_id"],
                variant=t["variant"],
                image_ref=t["image_ref"] or "unresolved",
                ttl_days=ttl_days,
            )
        )
    return {"enqueued": len(enqueued), "targets": targets, "enabled": True}


# ---------------------------------------------------------------------------
# Compare: fresh verdict vs incumbent verdict
# ---------------------------------------------------------------------------

def incumbent(db, node_definition_id: int, variant: str) -> Dict[str, Any]:
    """The incumbent verification for one (node, variant): the live
    ``NodeVariantVerification`` row's verdict + best_score, plus the
    ``NodeDefinition.variant_verification_status`` label. Empty dict when the
    variant has never been verified (nothing to compare against)."""
    import models
    row = (
        db.query(models.NodeVariantVerification)
        .filter(
            models.NodeVariantVerification.node_definition_id == node_definition_id,
            models.NodeVariantVerification.variant == variant,
            models.NodeVariantVerification.deleted_at.is_(None),
        )
        .first()
    )
    nd = (
        db.query(models.NodeDefinition)
        .filter(models.NodeDefinition.id == node_definition_id)
        .first()
    )
    status = None
    if nd is not None and isinstance(nd.variant_verification_status, dict):
        status = nd.variant_verification_status.get(variant)
    if row is None and not status:
        return {}
    return {
        "verdict": str(row.verdict) if row is not None else None,
        "best_score": (float(row.best_score)
                       if row is not None and row.best_score is not None else None),
        "status": status,
    }


def is_regression(fresh: Dict[str, Any], before: Dict[str, Any],
                  *, score_drop: Optional[float] = None) -> bool:
    """A regression is verified→failed/drift, or a score drop beyond the
    tolerance. Equal-or-better is never a regression: an incumbent that was
    already bad (drift/failed) and stays bad is not a NEW regression — the
    gate only blocks what it newly breaks."""
    drop = (score_drop if score_drop is not None else DEFAULT_SCORE_DROP)
    fresh_v = str(fresh.get("verdict") or "").lower()
    before_v = str(before.get("verdict") or "").lower()
    if before_v in _GOOD_VERDICTS and fresh_v in _BAD_VERDICTS:
        return True
    before_s = before.get("best_score")
    fresh_s = fresh.get("score")
    if before_s is not None and fresh_s is not None:
        return (float(before_s) - float(fresh_s)) > drop
    return False


def evaluate_on_change(
    db,
    targets: List[Dict[str, Any]],
    results: Dict[Tuple[int, str], Dict[str, Any]],
) -> Dict[str, Any]:
    """Compare each fresh sample-test result against the incumbent verdict.

    ``results`` maps ``(node_definition_id, variant)`` to the fresh
    ``{"verdict": ..., "score": ...}`` the sample-test landed. On a
    regression the change is marked: one ``VARIANT_CHANGE_REGRESSED``
    ``audit_events`` row carrying the offending variants, and
    ``promotion_blocked`` is True — the caller must not promote the change
    until an operator clears it. Equal-or-better results leave the flag and
    the audit trail untouched.
    """
    import models
    regressed: List[Dict[str, Any]] = []
    for t in targets:
        key = (int(t["node_definition_id"]), str(t["variant"]).lower())
        fresh = results.get(key)
        if not fresh:
            continue  # not landed yet — nothing to compare
        before = incumbent(db, key[0], key[1])
        if not before:
            continue  # no incumbent: first verification, nothing to regress
        if is_regression(fresh, before, score_drop=_score_drop(db)):
            regressed.append({
                "node_definition_id": key[0],
                "node_kind": t.get("node_kind"),
                "variant": key[1],
                "reason": t.get("reason"),
                "before": before,
                "after": fresh,
            })

    if not regressed:
        return {"regressed": False, "promotion_blocked": False,
                "regressed_variants": []}

    try:
        db.add(models.AuditEvent(
            tenant_id=None,
            user_id=None,
            event_name="VARIANT_CHANGE_REGRESSED",
            details={
                "reason": "corpus_gate_on_change_regression",
                "regressed_variants": regressed,
                "score_drop": _score_drop(db),
            },
        ))
    except Exception:  # audit is best-effort — blocking must still happen
        pass

    return {"regressed": True, "promotion_blocked": True,
            "regressed_variants": regressed}


# ---------------------------------------------------------------------------
# Weekly sweep + report (23.7.s2)
# ---------------------------------------------------------------------------

def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Naive DB timestamps read as UTC; everything else normalises."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ``attempt_history`` / metadata ISO timestamp string."""
    if isinstance(value, datetime):
        return _utc(value)
    if not value or not isinstance(value, str):
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _week_key(dt: datetime) -> str:
    """ISO year-week identity for one sweep window."""
    iso = _utc(dt).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _is_currently_bad(verdict: Optional[str], score: Optional[float],
                      drop: float) -> bool:
    """Is the variant in a bad state RIGHT NOW?

    Absolute-low, NOT a regression: a variant whose best score sits below
    ``1.0 - score_drop`` never had anything to drop FROM. The report lists
    these separately as ``below_threshold``; only s1's
    :func:`is_regression` (a fresh result that dropped past the tolerance vs
    the incumbent, or a bad verdict) counts as regressed.
    """
    if str(verdict or "").lower() in _BAD_VERDICTS:
        return True
    if score is not None and score < (1.0 - drop):
        return True
    return False


def _verification_row(db, node_definition_id: int,
                      variant: str) -> Optional[Any]:
    """The live ``NodeVariantVerification`` row for one (node, variant)."""
    import models
    return (
        db.query(models.NodeVariantVerification)
        .filter(
            models.NodeVariantVerification.node_definition_id == node_definition_id,
            models.NodeVariantVerification.variant == variant,
            models.NodeVariantVerification.deleted_at.is_(None),
        )
        .first()
    )


def _attempts_since(row: Any, since: Optional[datetime]) -> List[Dict[str, Any]]:
    """``attempt_history`` entries scored after ``since`` — the results the
    PREVIOUS sweep enqueued and a worker has since landed."""
    out: List[Dict[str, Any]] = []
    for entry in (getattr(row, "attempt_history", None) or []):
        if not isinstance(entry, dict):
            continue
        ts = _parse_iso(entry.get("scored_at"))
        if ts is None:
            continue
        if since is None or ts > since:
            out.append(entry)
    return out


def _state_before_window(row: Any, since: Optional[datetime]) -> Dict[str, Any]:
    """The (verdict, best_score) the variant carried when the window opened.

    Rebuilt from ``attempt_history`` because the row itself always describes
    the BEST attempt — a winning in-window attempt already overwrote it. No
    attempt before the window means nothing to regress against (first
    verification of that variant).
    """
    prior = [e for e in (getattr(row, "attempt_history", None) or [])
             if isinstance(e, dict) and _parse_iso(e.get("scored_at"))
             and _parse_iso(e.get("scored_at")) <= since]
    if not prior:
        return {}
    best = max(prior, key=lambda e: float(e.get("score") or 0.0))
    return {"verdict": best.get("verdict"), "best_score": best.get("score")}


def weekly_report(db, *, since: Optional[datetime] = None,
                  until: Optional[datetime] = None) -> Dict[str, Any]:
    """The weekly sweep report for one window.

    The window is ``since`` → ``until`` (default: the 7 days before now, or
    the interval since the previously persisted report — see
    :func:`run_weekly_sweep`). It describes results that ALREADY LANDED in
    that window, i.e. what the previous sweep enqueued, so it never reports
    the pre-sweep state as if it were the new one.

    ``regressed`` uses s1's :func:`is_regression` — a fresh result whose
    score dropped past ``node_build.variant_change_gate_score_drop`` vs the
    incumbent, or a good→bad verdict move. An incumbent that was already bad
    and stays bad is not a NEW regression. Variants that are merely
    absolute-low (bad verdict / score under ``1.0 - score_drop``) go in the
    separate ``below_threshold`` list — they never had anything to drop FROM,
    so calling them regressed would blame this sweep for a pre-existing
    condition.

    Returns ``{"window": {…}, "total": N, "passed": N, "regressed": N,
    "regressed_variants": […], "below_threshold": […]}``.
    """
    register_settings()
    if not _enabled(db):
        return {"total": 0, "passed": 0, "regressed": 0,
                "regressed_variants": [], "below_threshold": []}

    drop = _score_drop(db)
    until_dt = _utc(until) or datetime.now(timezone.utc)
    since_dt = _utc(since) or (until_dt - timedelta(days=7))

    targets = affected_variants(db, variant_change=True)

    regressed_variants: List[Dict[str, Any]] = []
    below_threshold: List[Dict[str, Any]] = []
    for t in targets:
        nd_id = t["node_definition_id"]
        variant = t["variant"]
        row = _verification_row(db, nd_id, variant)
        if row is None:
            continue
        inc = incumbent(db, nd_id, variant)
        fresh = _attempts_since(row, since_dt)
        before = _state_before_window(row, since_dt)
        hit = None
        for entry in fresh:
            if before and is_regression({"verdict": entry.get("verdict"),
                                         "score": entry.get("score")},
                                        before, score_drop=drop):
                hit = entry
                break
        if hit:
            regressed_variants.append({
                "node_definition_id": nd_id,
                "node_kind": t.get("node_kind"),
                "variant": variant,
                "reason": t.get("reason"),
                "before": before,
                "after": {"verdict": hit.get("verdict"),
                          "score": hit.get("score")},
            })
            continue
        if _is_currently_bad(inc.get("verdict"), inc.get("best_score"), drop):
            below_threshold.append({
                "node_definition_id": nd_id,
                "node_kind": t.get("node_kind"),
                "variant": variant,
                "reason": t.get("reason"),
                "verdict": inc.get("verdict"),
                "best_score": inc.get("best_score"),
            })

    regressed_variants.sort(key=lambda r: (r["node_definition_id"], r["variant"]))
    below_threshold.sort(key=lambda r: (r["node_definition_id"], r["variant"]))

    return {
        "window": {"since": since_dt.isoformat(),
                   "until": until_dt.isoformat(),
                   "week_key": _week_key(until_dt)},
        "total": len(targets),
        "passed": len(targets) - len(regressed_variants) - len(below_threshold),
        "regressed": len(regressed_variants),
        "regressed_variants": regressed_variants,
        "below_threshold": below_threshold,
    }


def _weekly_report_rows(db, *, lookback_days: int = 365) -> List[Any]:
    """ADMIN_SYSTEM_MAINTENANCE audit rows in the lookback window, newest
    first. The ``event`` key in audit_metadata is what separates THIS
    sweep's report from the other platform reports that share the label
    (services/support_ticket_report.py, services/base_image_weekly.py,
    services/privileged_elevation_summary.py) — same reason the SQL filter
    stays coarse: no report/digest AuditAction exists in the enum.
    """
    import models
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    try:
        return (
            db.query(models.PermissionAuditLog)
            .filter(models.PermissionAuditLog
                    .action == models.AuditAction.ADMIN_SYSTEM_MAINTENANCE,
                    models.PermissionAuditLog.timestamp >= since)
            .order_by(models.PermissionAuditLog.timestamp.desc(),
                      models.PermissionAuditLog.id.desc())
            .all()
        )
    except Exception:  # audit read failure — treat as "no history"
        return []


def _previous_sweep_at(db) -> Optional[datetime]:
    """When the last weekly report was persisted = the start of this sweep's
    window (results gathered since the previous sweep)."""
    for arow in _weekly_report_rows(db):
        md = arow.audit_metadata or {}
        if md.get("event") == WEEKLY_EVENT:
            ts = _utc(arow.timestamp)
            if ts is not None:
                return ts
    return None


def latest_weekly_report(db) -> Optional[Dict[str, Any]]:
    """The newest persisted weekly report, or None when there is none.

    Admins / API surfaces read this instead of recomputing: the report is
    the audit row, append-only (ISO 27001 — never deleted, so a rerun can
    never 'clear' it).
    """
    for arow in _weekly_report_rows(db):
        md = arow.audit_metadata or {}
        if md.get("event") != WEEKLY_EVENT:
            continue
        return {k: v for k, v in md.items() if k != "event"}
    return None


def _persist_weekly_report(db, report: Dict[str, Any]) -> Optional[Any]:
    """Append the report to the audit trail. Best-effort: a failed audit
    write must not stop the sweep that follows it."""
    import models
    from audit import log_security_event
    try:
        return log_security_event(
            db=db,
            action=models.AuditAction.ADMIN_SYSTEM_MAINTENANCE,
            metadata={"event": WEEKLY_EVENT, **report},
        )
    except Exception as exc:  # noqa: BLE001 — audit is best-effort
        _LOGGER.warning("variant_change_gate weekly report: audit row failed: %s",
                        exc)
        return None


def run_weekly_sweep(db) -> Dict[str, Any]:
    """Weekly sweep: REPORT first, then enqueue.

    Order matters: the report describes results that gathered SINCE THE
    PREVIOUS sweep (attempt_history entries landed after the last persisted
    report, compared with s1's :func:`is_regression` against the state each
    variant had when the window opened). Those runs were enqueued by the
    PREVIOUS sweep. Enqueueing this week's runs first would put the
    pre-sweep state in the report and blame it on this sweep.

    The report is persisted as an audit row before anything enqueues, so a
    crash mid-enqueue still leaves the report on the ledger, and
    :func:`latest_weekly_report` can show it to admins.

    Returns the report dict plus ``"enqueued"``.
    """
    register_settings()
    if not _enabled(db):
        return {"enqueued": 0, "total": 0, "passed": 0, "regressed": 0,
                "regressed_variants": [], "below_threshold": []}

    since = _previous_sweep_at(db)
    report = weekly_report(db, since=since)
    _persist_weekly_report(db, report)

    from .queue import enqueue_pending
    targets = affected_variants(db, variant_change=True)
    enqueued: List[int] = []
    for t in targets:
        enqueued.append(
            enqueue_pending(
                db,
                build_spec_id=None,
                node_definition_id=t["node_definition_id"],
                variant=t["variant"],
                image_ref=t["image_ref"] or "unresolved",
                ttl_days=30,
            )
        )

    return {**report, "enqueued": len(enqueued)}


# The gate's tunables must be LIVE in the platform-settings registry on import,
# not only when a gate call happens to run: GET /platform/settings enumerates
# the registry and PATCH /platform/settings/{key} resolves through it, so a
# lazily-registered key is invisible to the admin surface (and cannot be
# enabled) until some sweep or hook fires first — a catch-22 for a dark-by-
# default gate whose only sweep is itself gated on the flag. Same import-time
# idiom as every other settings module (services/credit_tier_thresholds.py:187,
# services/agent_node_runner.py:58). register() is idempotent, so the in-call
# register_settings() guards above stay.
register_settings()

