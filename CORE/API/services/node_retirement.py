"""FX.103 part 2 (owner decision Q89=A) — node retirement scheduler + run
guard.

Part 1 (services/node_deprecation.py) owns the lifecycle WRITES (status
'active' | 'deprecated' | 'retired' on node_definitions, validated
deprecated_at / retire_after, audited flips). This part owns the RUN-TIME
enforcement:

* run_retirement_sweep — the daily APScheduler tick (services/scheduler.py,
  job id 'fx103_node_retirement', 04:05 UTC):
    (a) finds nodes marked 'deprecated' that carry a retirement date
        (retire_after);
    (b) REFUSES to retire anything whose notice period
        (lifecycle.notice_months, default 6) or runnable floor
        (lifecycle.min_runnable_months, default 12) has not elapsed;
    (c) sends the 6-month notice email ONCE to the tenant admins of every
        tenant whose workflows use the node (utils/email_sender
        send_templated_email, template 'deprecation-notice'); the send is
        stamped on node_definitions.deprecation_notice_sent_at so it never
        re-sends;
    (d) on the retirement date flips status to 'retired' (SOFT — nothing
        is deleted) and writes the NODE_RETIRED audit row via the part-1
        apply_node_status single write path.

  Models are out of scope this pass: there is no model-catalog table yet
  (models are served by persistent_stacks / nim_catalog's coarse
  is_active), so the lifecycle lands on node definitions only.

* run_guard_violation — the RETIREMENT RUN GUARD, called from the dispatch
  path (routers/worker_tasks.enqueue_task_internal) at the pinned-version
  resolution point: pinned runs keep working until the retirement date,
  then the caller fails the run with the clear 'retired on <date>; upgrade
  via POST /workflows/{id}/upgrade-nodes' error.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import models
from sqlalchemy.orm import Session

try:
    from services import node_deprecation as _dep
except (ImportError, ModuleNotFoundError):
    _dep = None

logger = logging.getLogger(__name__)

NOTICE_TEMPLATE_NAME = "deprecation-notice"


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Naive timestamps (legacy rows) are read as UTC; aware stay as-is."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _tenants_using_node(db: Session, node_definition_id: int) -> list:
    """Distinct tenant ids of every non-deleted workflow that has a node
    on this definition (WorkflowNode -> Workflow.tenant_id)."""
    rows = (
        db.query(models.Workflow.tenant_id)
        .join(models.WorkflowNode,
              models.WorkflowNode.workflow_id == models.Workflow.id)
        .filter(models.WorkflowNode.node_definition_id == node_definition_id,
                models.Workflow.deleted_at.is_(None))
        .distinct()
        .all()
    )
    return sorted({r[0] for r in rows if r[0] is not None})


def _send_notice(db: Session, nd: "models.NodeDefinition",
                 now: datetime) -> tuple:
    """Send the one-time deprecation notice. Returns (done, sent_count).

    done=True means the notice is DONE for this node: either delivered to
    every reachable admin, or there was nobody to send to (no tenant
    workflows on the node, or no admin with an email) — the caller stamps
    deprecation_notice_sent_at either way so the daily sweep stops
    re-scanning. done=False means at least one email dispatch RAISED (e.g.
    the 'deprecation-notice' template is not yet seeded in MinIO —
    send_templated_email audit-emits EMAIL_TEMPLATE_MISSING itself); the
    stamp stays NULL and the next tick retries.
    """
    # 1788k CE carve: crud/stripe_webhooks* is PAID (billing domain), so CE has
    # no admin-lookup helper. There is no Stripe/billing side effect to run here
    # — the deprecation notice simply has nobody to fan out to, so the sweep
    # stamps the notice DONE and retirement itself still proceeds unchanged.
    from utils.paid_import import optional_paid
    _stripe_webhooks = optional_paid("crud.stripe_webhooks")
    list_active_tenant_admin_users = (
        _stripe_webhooks.list_active_tenant_admin_users
        if _stripe_webhooks is not None else (lambda db, tid: [])
    )
    tenant_ids = _tenants_using_node(db, nd.id)
    recipients = []  # (tenant_id, email, display_name)
    for tid in tenant_ids:
        for admin in list_active_tenant_admin_users(db, tid):
            if admin.email:
                recipients.append(
                    (tid, admin.email,
                     admin.first_name or admin.username or ""))
    if not recipients:
        return True, 0

    retired_on = _utc(nd.retire_after) or now
    props = {
        "node_name": nd.name or f"node {nd.id}",
        "deprecated_on": (_utc(nd.deprecated_at) or now).strftime("%B %d, %Y"),
        "retire_after": retired_on.strftime("%B %d, %Y"),
        "affected_workflows": len(tenant_ids),
        "upgrade_endpoint": "POST /workflows/{id}/upgrade-nodes",
    }
    subject = (f"Deprecation notice: '{nd.name}' retires on "
               f"{retired_on.strftime('%B %d, %Y')}")
    sent = 0
    for tid, email, name in recipients:
        try:
            from utils.email_sender import send_templated_email
            send_templated_email(
                db=db, tenant_id=tid, template_name=NOTICE_TEMPLATE_NAME,
                props=props, recipient_email=email, recipient_name=name,
                subject=subject,
            )
            sent += 1
        except Exception as e:  # noqa: BLE001 — one bad send must not eat
            # the rest; the sweep retries (stamp stays NULL).
            logger.warning("FX.103 deprecation notice to %s failed: %s",
                           email, e)
            if e.__class__.__name__ == "EmailTemplateMissing":
                break  # template absent = every send fails identically
    return sent == len(recipients), sent


def run_retirement_sweep(db: Session,
                         now: Optional[datetime] = None) -> dict:
    """One daily tick (services/scheduler.py job 'fx103_node_retirement').

    Returns counters {checked, notices_done, notice_emails, retired,
    refused}. Owns its commit (scheduler hands it a fresh session)."""
    if _dep is None:  # CE build: services.node_deprecation is manifest-denied
        logger.info("node retirement sweep skipped "
                    "(services.node_deprecation not in this edition)")
        return {"checked": 0, "notices_done": 0, "notice_emails": 0,
                "retired": 0, "refused": 0}
    now = _utc(now) or datetime.now(timezone.utc)
    min_runnable = _dep.min_runnable_months(db)
    notice_m = _dep.notice_months(db)
    counters = {"checked": 0, "notices_done": 0, "notice_emails": 0,
                "retired": 0, "refused": 0}
    candidates = (
        db.query(models.NodeDefinition)
        .filter(models.NodeDefinition.status.in_(
            (_dep.NODE_STATUS_DEPRECATED, _dep.NODE_STATUS_RETIRED)),
            models.NodeDefinition.retire_after.isnot(None),
            models.NodeDefinition.deleted_at.is_(None))
        .all()
    )
    for nd in candidates:
        counters["checked"] += 1
        dep_at = _utc(nd.deprecated_at)
        ra = _utc(nd.retire_after)
        if dep_at is None or ra is None:
            continue

        # (c) the one-time notice — while the node is still runnable and
        # its notice hasn't been stamped.
        if (nd.status == _dep.NODE_STATUS_DEPRECATED
                and getattr(nd, "deprecation_notice_sent_at", None) is None
                and now >= dep_at and now < ra):
            try:
                done, sent = _send_notice(db, nd, now)
            except Exception as e:  # noqa: BLE001 — sweep must never die
                logger.exception("FX.103: notice dispatch for node %s "
                                 "raised: %s", nd.id, e)
                done, sent = False, 0
            counters["notice_emails"] += sent
            if done:
                nd.deprecation_notice_sent_at = now
                counters["notices_done"] += 1

        # (b)+(d) on the retirement date: flip to retired — but REFUSE
        # while either policy floor has not elapsed.
        if nd.status != _dep.NODE_STATUS_RETIRED and now >= ra:
            notice_floor = _dep.months_after(dep_at, notice_m)
            runnable_floor = _dep.months_after(dep_at, min_runnable)
            if now < notice_floor or now < runnable_floor:
                counters["refused"] += 1
                logger.warning(
                    "FX.103: REFUSING to retire node %s (%s) — notice "
                    "period (%d mo) / %d-month runnable floor not yet "
                    "elapsed (deprecated_at=%s, retire_after=%s)",
                    nd.id, nd.name, notice_m, min_runnable,
                    dep_at.isoformat(), ra.isoformat())
                continue
            # The row's own timestamps are passed so the part-1 validator
            # runs against them (apply_node_status stamps only after the
            # validation).
            _dep.apply_node_status(
                db, nd, status=_dep.NODE_STATUS_RETIRED,
                deprecated_at=dep_at, retire_after=ra, actor_user=None)
            counters["retired"] += 1
            logger.info("FX.103: retired node %s (%s) — pinned workflows "
                        "now blocked at dispatch", nd.id, nd.name)
    db.commit()
    return counters


def run_guard_violation(db: Session,
                        node_run_id: Optional[int]) -> Optional[dict]:
    """The RETIREMENT RUN GUARD (called from the dispatch path at the
    pinned-version resolution point, routers/worker_tasks
    enqueue_task_internal).

    Returns {name, node_definition_id, workflow_id, workflow_run_id,
    retired_on} when the node_run's node definition must no longer
    execute — status 'retired', or 'deprecated' past retire_after — and
    None while it is still runnable (active always; deprecated/retired
    until retire_after per the part-1 is_runnable rule). Pinned or not:
    a retired node is not runnable for ANY new dispatch.

    Fail-open on lookup hiccups (transient DB error → None + log): the
    neighbouring pin resolution is best-effort by design, and a DB that
    cannot answer this query cannot dispatch anything anyway.
    """
    if not node_run_id:
        return None
    if _dep is None:
        logger.info("node_deprecation guard module not in this edition — skip")
        return None
    try:
        nr = (db.query(models.NodeRun)
              .filter(models.NodeRun.id == node_run_id).first())
        if not nr:
            return None
        wn = (db.query(models.WorkflowNode)
              .filter(models.WorkflowNode.id == nr.workflow_node_id).first())
        if not wn or not wn.node_definition_id:
            return None
        nd = (db.query(models.NodeDefinition)
              .filter(models.NodeDefinition.id == wn.node_definition_id)
              .first())
        if not nd or _dep.is_runnable(nd):
            return None
        return {
            "name": nd.name or f"node {nd.id}",
            "node_definition_id": nd.id,
            "workflow_id": wn.workflow_id,
            "workflow_run_id": nr.workflow_run_id,
            "retired_on": _utc(nd.retire_after) or _utc(nd.deprecated_at),
        }
    except Exception:  # noqa: BLE001
        logger.exception("FX.103: run-guard lookup failed for node_run %s",
                         node_run_id)
        return None
