"""project-agent §6.1 — handing work to the wizards, and whose authority it runs under.

THE TREE (§6.1). Every wizard conversation the agent holds is a child of
the project session. `SupportChatSession.parent_session_id` already
existed, so this needed no schema change — and the tree is the whole
mechanism by which the agent holds context the wizards do not. The node
wizard keeps receiving exactly what it has always needed: expected input,
the process, expected output. The reasoning about WHY that node is wanted
stays with the parent, which is what keeps the wizards cheap.

WHOSE AUTHORITY (§6.1a). The agent acts as its project's user, which is
how every non-interactive run on this platform already works:
`WorkflowRun.triggered_by_user_id` is the authority a run acts under and
`triggered_by_trigger_id` records what caused it, so a scheduled run names
both. The agent slots into the same shape — `project_session_id` is the
provenance, so a non-null value on a run means "the agent dispatched this
for that user". No new identity concept, and no new column.

Its reach is therefore exactly its user's reach, which also settles what
it may do with stacks: it holds whatever capability its user holds and
cannot become a privilege-escalation path.

WAKING AFTER A LONG PARK (§6.1b). Long-RUNNING work is already solved:
the dashboard logs in with scope=offline_access and an offline session's
idle clock resets on every use, so a photogrammetry run calling
exchange-token for weeks keeps it alive indefinitely.

Long-IDLE is the new case, and park-and-notify creates it deliberately. A
project parked on a question for five weeks makes no calls at all, so
nothing resets that clock. The answer is re-authentication on wake rather
than a heartbeat: keeping an offline session alive for a project nobody
has touched in months is a standing grant of the user's authority for work
they may well have abandoned. Answering a question after five weeks and
being asked to sign in is unsurprising; discovering that something held
your authority for five weeks is not.

Nothing durable depends on the credential, so re-authenticating RESUMES
the work rather than restarting it (§6.1c).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


PROJECT_KIND = "project"

# Keycloak realm config (`keycloak-init.sh`) sets offlineSessionIdleTimeout
# to 30 days, and offlineSessionMaxLifespanEnabled is pinned false so only
# the idle timer applies. Mirrored here so the two cannot drift silently;
# if that realm setting changes this must change with it.
OFFLINE_IDLE_SECONDS = 30 * 24 * 60 * 60

# Margin before the real expiry at which a wake is treated as needing
# re-authentication. Waking a project into a token that dies mid-build is
# worse than asking one day early.
REAUTH_MARGIN_SECONDS = 24 * 60 * 60


class ProjectCredentialsLapsed(Exception):
    """The project's user must sign in again before work is dispatched.

    Not a failure of the work — the plan, criteria, example and failure
    history are all intact and none of them depend on the credential.
    """


def run_authority(session) -> int:
    """The user whose authority a dispatched run acts under.

    §6.1a — the agent has no identity of its own. It acts as the person
    whose project it is, so its reach is exactly their reach.
    """
    return session.user_id


def stamp_run(run, session) -> None:
    """Record who a run acts for and what caused it.

    `triggered_by_user_id` is the AUTHORITY, matching every other
    non-interactive run. `project_session_id` is the PROVENANCE — a
    non-null value means the agent dispatched this, which is what lets a
    log distinguish work the user did from work the agent did for them.
    """
    run.triggered_by_user_id = run_authority(session)
    run.project_session_id = session.id


def last_activity(session) -> Optional[datetime]:
    """When this project last did anything that would reset an offline
    session's idle clock.

    `last_message_at` is the activity marker and is maintained on every
    turn; `created_at` is only a fallback for a session that has not had
    one yet. NOT max() of the two — that would make a project whose row
    was created recently but which has been silent for months look fresh,
    which is exactly the case this exists to catch.
    """
    stamp = session.last_message_at or session.created_at
    if stamp is None:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def credentials_presumed_lapsed(session, *, now: Optional[datetime] = None) -> bool:
    """Whether this project has been idle long enough that its user's
    offline session has expired.

    A PRESUMPTION, and named as one: the platform cannot see Keycloak's
    session table from here, and the only way to learn the truth is to try
    a token exchange and be refused. Presuming from idle time lets the
    agent ask up front rather than discovering it mid-dispatch, and
    presuming a day early (REAUTH_MARGIN_SECONDS) is cheaper than waking
    into a token that dies mid-build.
    """
    now = now or datetime.now(timezone.utc)
    seen = last_activity(session)
    if seen is None:
        return False
    idle = (now - seen).total_seconds()
    return idle >= (OFFLINE_IDLE_SECONDS - REAUTH_MARGIN_SECONDS)


def wake(session, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """§6.1b — what happens when a parked project is picked back up.

    Returns a decision rather than acting, so an operator can ask what a
    project would do on wake without waking it.
    """
    if credentials_presumed_lapsed(session, now=now):
        seen = last_activity(session)
        return {
            "action": "reauthenticate",
            "project_session_id": session.id,
            "idle_days": int(((now or datetime.now(timezone.utc)) - seen).days)
            if seen else None,
            "reason": (
                "This project has been idle long enough that your sign-in has "
                "expired. Nothing has been lost — its plan, acceptance "
                "criteria, example and the record of what has already been "
                "tried are all intact. Signing in resumes it where it stopped."
            ),
            "context_intact": True,
        }
    return {
        "action": "resume",
        "project_session_id": session.id,
        "context_intact": True,
    }


def assert_can_dispatch(session, *, now: Optional[datetime] = None) -> None:
    """Guard for the dispatch loop.

    Refuses rather than degrading: an agent that keeps working on stale
    authority is worse than one that stops and says why.
    """
    if credentials_presumed_lapsed(session, now=now):
        raise ProjectCredentialsLapsed(wake(session, now=now)["reason"])


# --- what each wizard is told ----------------------------------------------

def node_wizard_payload(task: Dict[str, Any], criteria: Optional[Dict[str, Any]] = None,
                        *, avoid: Optional[List[str]] = None,
                        guidance: Optional[str] = None) -> Dict[str, Any]:
    """§6.1 — what the NODE wizard is given for one task.

    Expected input, the process needed, expected output. That is all it
    has ever needed and all it gets: the project's conversation, its other
    tasks, and the reasoning behind this one stay with the parent.

    `sample_output` rides along when the project supplied an example,
    because that is what makes the built node's publish gate load-bearing
    (§1.1) rather than "the container exited 0".

    `guidance` is a human answer to a parked question (§7.4), carried into
    the attempt it unblocks. It is this project's own material — the user's
    reply about this task — so it does not breach the ownership boundary
    (§8.3); no other tenant's material is ever placed here.
    """
    payload: Dict[str, Any] = {
        "node_name": task.get("label") or task.get("key"),
        "purpose": task.get("goal"),
        "input_schema_hint": task.get("input_schema_hint"),
        "output_schema_hint": task.get("output_schema_hint"),
    }
    if avoid:
        # Not context about the project — context about this node's own
        # history, which is the one thing that stops a repeat.
        payload["avoid_approaches"] = list(avoid)
    if guidance:
        payload["human_guidance"] = guidance
    if criteria:
        sample_in = criteria.get("input")
        sample_out = criteria.get("output")
        if sample_in:
            payload["sample_input"] = sample_in
        if sample_out:
            payload["sample_output"] = sample_out
    return payload


# --- §8 ownership: scoping is at the query, not a rule in a document -------
#
# These are pure functions the discovery/candidate code calls to build its
# WHERE clause. Keeping the scope here — one place, next to what dispatches
# the work — is what lets "another tenant's artifact is INVISIBLE, not merely
# unusable" (§8.1/§8.2) be enforced structurally rather than trusted. The
# discovery queries themselves live in node_builder and the wizard, which
# apply this scope; this module owns the definition of the scope.

def is_confidential(session) -> bool:
    """§8.7 — whether this project is walled off from the tenant's shared
    learning. Off by default: a project participates in tenant-wide learning
    unless it is explicitly marked confidential.

    Stored on the plan JSONB (``_confidential``) rather than a column so it
    needs no schema change; a first-class column is the cleaner home and is
    left to the owner. Survey and AEC firms hold work for competing clients,
    where cross-project learning is a contract breach rather than a feature.
    """
    plan = session.project_plan
    return bool(isinstance(plan, dict) and plan.get("_confidential"))


def discovery_scope(session) -> Dict[str, Any]:
    """§8.1/§8.2 — the scope every artifact-discovery query the agent runs
    must apply, expressed so it is applied in the query and not by filtering
    afterwards.

    Reusable for any tenant: the platform catalog, MOD's system nodes and
    base images, MOD's documentation (§8, D8). Never reusable: anything
    another tenant produced. So the scope is exactly "this tenant OR the
    platform catalog", and there is deliberately no third option — no
    promotion path exists (§8.4) because nothing here can widen the scope
    to another tenant.
    """
    return {
        "tenant_id": session.tenant_id,
        "include_platform_catalog": True,
        # Never true: this is the promotion path that §8.4 asserts absent.
        "include_other_tenants": False,
    }


def learning_scope(session) -> Dict[str, Any]:
    """§8.6/§8.7 — whose prior work may inform this project.

    Tenant-wide by default: one department's failures are the same
    company's failures, so a tenant learns across ALL their projects. A
    confidential project narrows to itself alone — it draws on nothing
    outside it and contributes nothing to the shared pool.
    """
    if is_confidential(session):
        return {
            "tenant_id": session.tenant_id,
            "project_session_id": session.id,
            "scope": "project_only",
            "include_platform_catalog": True,
        }
    return {
        "tenant_id": session.tenant_id,
        "project_session_id": None,  # all of the tenant's projects
        "scope": "tenant_wide",
        "include_platform_catalog": True,
    }


def workflow_wizard_payload(session, *, goal: Optional[str] = None) -> Dict[str, Any]:
    """§6.1 — what the WORKFLOW wizard is given.

    More than the node wizard gets, because composing a graph needs the
    shape of the whole need — but still the need, not the transcript.
    """
    criteria = session.project_criteria or {}
    return {
        "goal": goal or session.project_need,
        "described_outcome": criteria.get("described_outcome"),
        "input_example": (criteria.get("input") or {}).get("filename"),
        "output_example": (criteria.get("output") or {}).get("filename"),
    }


def open_child_session(db, session, kind: str, *, title: Optional[str] = None):
    """Start a wizard conversation belonging to this project.

    The child inherits the project's user and tenant — it acts under the
    same authority, because the agent has none of its own.
    """
    import models
    child = models.SupportChatSession(
        id=str(uuid.uuid4()),
        user_id=session.user_id,
        tenant_id=session.tenant_id,
        kind=kind,
        parent_session_id=session.id,
        title=title,
    )
    db.add(child)
    db.flush()
    return child


def child_sessions(db, session, *, kind: Optional[str] = None) -> List[Any]:
    import models
    q = (db.query(models.SupportChatSession)
         .filter(models.SupportChatSession.parent_session_id == session.id))
    if kind:
        q = q.filter(models.SupportChatSession.kind == kind)
    return q.order_by(models.SupportChatSession.created_at.asc()).all()
