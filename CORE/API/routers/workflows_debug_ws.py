"""routers/workflows_debug_ws.py -- live-debugger sz-2.4 (opsx live-debugger).

WebSocket  /v1/road/workflow-runs/{run_id}/debug -- the attach channel the
canvas debugger talks to. JSON text frames both ways:

  client -> {"id": str, "cmd": <verb>, "node_id": str, "args": {...}}
  server -> {"event": "command_ack",   "id": ..., "result": {...}}
            {"event": "command_error", "id": ..., "code": ..., "message": ...}
            {"event": "frame_state",   "id": ..., "frame": {...}}  (inspect)

Verbs: set_breakpoint | clear_breakpoint | inspect | hot_swap | resume |
step_over | step_into. `inspect` answers with `frame_state` (never an ack) so
the client can tell a frame from a mutation.

Auth is the single-use `?ticket=` path the SSE channels already use
(services/stream_tickets.redeem_ticket): the ticket carries no identity claims
of its own and is consumed once, so nothing sensitive sits in a proxy access
log. Redeeming gives {user_id, tenant_id}; the user is re-checked ACTIVE and
tenant-resident, and the run is resolved tenant-scoped + per-workflow view
scope -- exactly the authorization routers/workflows_debug.py uses -- in a
SHORT-LIVED session that closes before the socket opens.

Close codes: 4401 no/invalid ticket, 4403 user not active / run not visible
(identical for a foreign run and an unknown one -- no oracle), 1009 frame over
64 KiB. Everything else stays a `command_error` on a live socket.

`resume` is answered `command_error code="use_http_resume"`: the resume verb
(re-open the frontier + re-kick dispatch) only exists inline in
POST /v1/road/workflow-runs/{run_id}/resume-node -- there is no service
function to call from here, and duplicating that route's body in a second
place is the thing this vertical must not do.

sz-2.1 (the `workflow_breakpoints` table) has not landed, so
set_breakpoint / clear_breakpoint answer `breakpoints_unavailable` explicitly
-- never a silent ack. Server-push of `breakpoint_hit` is sz-2.5 (the
orchestrator's); `publish_debug_event` below is the seam it will hang on.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, Depends, WebSocket, WebSocketException

import models  # noqa: E402
from crud import get_user_by_id_for_tenant, get_workflow_run_by_id
from database import SessionLocal
from routers.workflows_domains._shared import _user_can_view_run
try:
    from services import live_debugger
except (ImportError, ModuleNotFoundError):
    live_debugger = None
try:
    from services.productivity_floor_rollout_dep import assert_rollout_ws
except (ImportError, ModuleNotFoundError):
    assert_rollout_ws = None  # type: ignore[assignment]
from services.stream_tickets import redeem_ticket

logger = logging.getLogger(__name__)

RUN_NOT_FOUND = "Workflow run not found"

MAX_FRAME_BYTES = 64 * 1024
RATE_LIMIT_CMDS_PER_SECOND = 20
CLOSE_NO_TICKET = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_TOO_LARGE = 1009
DEBUG_TICKET_SCOPE = "workflow-debug"

_INSPECT_CMDS = frozenset({"inspect"})
_MUTATING_CMDS = frozenset({"hot_swap", "resume", "step_over", "step_into"})
_BREAKPOINT_CMDS = frozenset({"set_breakpoint", "clear_breakpoint"})
_KNOWN_CMDS = _INSPECT_CMDS | _MUTATING_CMDS | _BREAKPOINT_CMDS

router = APIRouter(prefix="/road", tags=["The Road", "Workflow Debug WS"])


# ---------------------------------------------------------------------------
# Auth: redeem the single-use ticket, then re-check identity + run visibility.
# Returns a marker instead of raising: a WS handshake that fails must CLOSE
# (4401 / 4403), and FastAPI turns a dependency exception into a protocol
# error, not a close code. The handler closes from the marker.
# ---------------------------------------------------------------------------


def debug_ws_token(ticket: str = None, run_id: int = None) -> Dict[str, Any]:
    """Resolve the `?ticket=` into (user, tenant, run) or a close directive.

    R2 note: this IS the auth dependency (the name carries "token"); the
    socket has no Authorization header, the ticket is the credential.
    """
    if not ticket:
        return {"ok": False, "close_code": CLOSE_NO_TICKET,
                "close_reason": "ticket required"}
    payload = redeem_ticket(ticket)
    if not payload:
        return {"ok": False, "close_code": CLOSE_NO_TICKET,
                "close_reason": "invalid ticket"}
    # Scope gate: a debug socket only accepts a debug ticket. Without this a
    # minted "notifications" / "wizard-run" ticket would open the debugger
    # (fail-open). "workflow-debug" tickets are minted via
    # POST /v1/stream-tickets {"scope": "workflow-debug", "run_id": ...}
    # (stream_tickets.VALID_SCOPES + the mint branch in
    # routers/stream_tickets.py), which checks the same run-view access as
    # this socket before issuing one.
    if payload.get("scope") != DEBUG_TICKET_SCOPE:
        return {"ok": False, "close_code": CLOSE_NO_TICKET,
                "close_reason": "invalid ticket"}
    try:
        user_id = int(payload.get("user_id"))
        tenant_id = int(payload.get("tenant_id"))
    except (TypeError, ValueError):
        return {"ok": False, "close_code": CLOSE_NO_TICKET,
                "close_reason": "invalid ticket"}

    # Run binding: a ticket minted for one run must not open another run's
    # socket. A ticket that carries no run_id is scoped by user+tenant only.
    ticket_run = payload.get("run_id")
    if ticket_run not in (None, ""):
        try:
            if int(ticket_run) != int(run_id):
                return {"ok": False, "close_code": CLOSE_FORBIDDEN,
                        "close_reason": RUN_NOT_FOUND}
        except (TypeError, ValueError):
            return {"ok": False, "close_code": CLOSE_FORBIDDEN,
                    "close_reason": RUN_NOT_FOUND}

    session = SessionLocal()
    try:
        user = get_user_by_id_for_tenant(session, user_id, tenant_id)
        if user is None or user.status != models.UserStatus.ACTIVE:
            return {"ok": False, "close_code": CLOSE_FORBIDDEN,
                    "close_reason": RUN_NOT_FOUND}
        db_run = get_workflow_run_by_id(session, run_id=run_id, tenant_id=tenant_id)
        if not db_run or not _user_can_view_run(session, user, db_run):
            # Foreign run and unknown run are the same close: no oracle.
            return {"ok": False, "close_code": CLOSE_FORBIDDEN,
                    "close_reason": RUN_NOT_FOUND}
    finally:
        session.close()
    return {"ok": True, "user_id": user_id, "tenant_id": tenant_id,
            "run_id": run_id}


# ---------------------------------------------------------------------------
# Per-command sessions
# ---------------------------------------------------------------------------


def _open_session():
    """A fresh Session for ONE command (never held across the socket).

    The SSE channels already open their own short-lived `database.SessionLocal()`
    for exactly this reason: under pgbouncer transaction pooling a session that
    stays checked out for the socket's lifetime pins a connection for the whole
    attach. Same here -- one open/close per command.
    """
    return SessionLocal()


def _run_command(cmd: str, *, tenant_id: int, run_id: int, node_id: str,
                 user_id: int, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Execute one verb in its own session. Returns (event, payload).

    Commits happen inside the service (hot_swap / step_*); a refusal rolls the
    command's session back so a half-applied write never reaches the socket.
    """
    session = _open_session()
    try:
        # Per-command re-authorization: the socket was authorized once at
        # accept-time, but a deactivation or a revoked grant mid-socket must
        # not keep working. Same check the HTTP twin (workflows_debug.py)
        # applies on every request, run in THIS command's own session.
        user = get_user_by_id_for_tenant(session, user_id, tenant_id)
        db_run = get_workflow_run_by_id(session, run_id=run_id, tenant_id=tenant_id)
        if (user is None or user.status != models.UserStatus.ACTIVE
                or not db_run or not _user_can_view_run(session, user, db_run)):
            return "forbidden", {"code": "forbidden"}

        if cmd == "inspect":
            if live_debugger is None:
                return "command_error", {"code": "paid_feature",
                                          "message": "live debugger not available"}
            frame = live_debugger.breakpoint_service.inspect_frame(
                session, tenant_id=tenant_id, run_id=run_id, node_id=node_id)
            return "frame_state", {"frame": frame}
        if cmd == "hot_swap":
            if live_debugger is None:
                return "command_error", {"code": "paid_feature",
                                          "message": "live debugger not available"}
            result = live_debugger.breakpoint_service.hot_swap(
                session, tenant_id=tenant_id, run_id=run_id, node_id=node_id,
                actor_user_id=user_id,
                config=args.get("config"),
                node_definition_id=args.get("node_definition_id"))
            return "command_ack", {"result": result}
        if cmd == "step_over":
            if live_debugger is None:
                return "command_error", {"code": "paid_feature",
                                          "message": "live debugger not available"}
            result = live_debugger.breakpoint_service.step_over(
                session, tenant_id=tenant_id, run_id=run_id, node_id=node_id,
                actor_user_id=user_id)
            return "command_ack", {"result": result}
        if cmd == "step_into":
            if live_debugger is None:
                return "command_error", {"code": "paid_feature",
                                          "message": "live debugger not available"}
            result = live_debugger.breakpoint_service.step_into(
                session, tenant_id=tenant_id, run_id=run_id, node_id=node_id,
                actor_user_id=user_id)
            return "command_ack", {"result": result}
    except LookupError:
        session.rollback()
        # Same generic shape as the HTTP debug routes: a foreign run must not be
        # nameable either.
        return "command_error", {"code": "not_found", "message": RUN_NOT_FOUND}
    except RuntimeError as refused:
        session.rollback()
        # The service's own refusal text ("node is not paused" / "node already
        # dispatched") is curated, not raw exception text -- same as 409 on the
        # HTTP twin.
        return "command_error", {"code": "conflict", "message": str(refused)}
    except ValueError as bad:
        session.rollback()
        return "command_error", {"code": "unprocessable", "message": str(bad)}
    finally:
        session.close()
    return "command_error", {"code": "bad_request", "message": "unknown command"}


@router.websocket("/workflow-runs/{run_id}/debug")
async def debug_run_attach(
    websocket: WebSocket,
    run_id: int = None,
    ticket: str = None,
    auth: Dict[str, Any] = Depends(debug_ws_token),
):
    """Attach the live debugger to one run (sz-2.4).

    Authorization: single-use `?ticket=` -> user re-checked ACTIVE + tenant
    resident -> run resolved tenant-scoped + per-workflow view scope, identical
    to routers/workflows_debug.py's 404 pair. Failures CLOSE the handshake
    (4401 no/invalid ticket, 4403 not active / not visible) before accept-time
    work, so a rejected attach never looks like a live socket.

    Every command runs in its own DB session; the socket holds none.
    """
    if not auth.get("ok"):
        await websocket.close(code=int(auth["close_code"]),
                              reason=str(auth.get("close_reason", "")))
        return

    tenant_id, user_id, run_id = auth["tenant_id"], auth["user_id"], auth["run_id"]

    # sz-8 rollout gate (productivity-floor): the ticket already proved
    # authorization, this is the ADDITIONAL per-tenant rollout layer. It runs
    # after the tenant is resolved and BEFORE accept(), so a tenant the phase
    # has not reached never sees a live socket and never gets a frame of run
    # data -- the handshake is refused with the spec's policy-violation close
    # (1008) plus the gate's own reason. Short-lived session like every other
    # DB touch here: under pgbouncer pooling nothing may stay checked out
    # across the socket.
    try:
        gate_session = _open_session()
        try:
            if assert_rollout_ws is not None:
                assert_rollout_ws(gate_session, tenant_id, "live_debugging")
        finally:
            gate_session.close()
    except WebSocketException as refused:
        await websocket.close(code=int(refused.code),
                              reason=str(refused.reason or ""))
        return

    await websocket.accept()

    stamps: List[float] = []
    while True:
        msg = await websocket.receive()
        if msg["type"] == "websocket.disconnect":
            return
        raw = msg.get("text")
        if raw is None:
            raw = msg.get("bytes") or b""
            try:
                raw = raw.decode("utf-8")
            except (AttributeError, UnicodeDecodeError):
                raw = ""
        if len(raw.encode("utf-8", "ignore")) > MAX_FRAME_BYTES:
            await websocket.close(code=CLOSE_TOO_LARGE, reason="frame too large")
            return

        try:
            envelope = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            envelope = None
        if (not isinstance(envelope, dict) or not isinstance(envelope.get("id"), str)
                or not envelope.get("id") or not isinstance(envelope.get("cmd"), str)):
            await websocket.send_json({"event": "command_error", "id": "",
                                       "code": "bad_request",
                                       "message": "envelope needs a string id and cmd"})
            continue

        cmd_id = envelope["id"]
        cmd = envelope["cmd"]
        node_id = envelope.get("node_id")
        args = envelope.get("args") or {}
        if not isinstance(args, dict):
            await websocket.send_json({"event": "command_error", "id": cmd_id,
                                       "code": "bad_request",
                                       "message": "args must be an object"})
            continue

        now = time.monotonic()
        stamps[:] = [t for t in stamps if now - t < 1.0]
        if len(stamps) >= RATE_LIMIT_CMDS_PER_SECOND:
            await websocket.send_json({"event": "command_error", "id": cmd_id,
                                       "code": "rate_limited",
                                       "message": "too many commands"})
            continue
        stamps.append(now)

        if cmd not in _KNOWN_CMDS:
            await websocket.send_json({"event": "command_error", "id": cmd_id,
                                       "code": "bad_request",
                                       "message": "unknown command"})
            continue
        if cmd in _BREAKPOINT_CMDS:
            # sz-2.1 (workflow_breakpoints) has not landed: explicit, never a
            # silent ack.
            await websocket.send_json({"event": "command_error", "id": cmd_id,
                                       "code": "breakpoints_unavailable",
                                       "message": "breakpoint table not available"})
            continue
        if cmd == "resume":
            # Resume (clear the pause + re-kick the frontier) lives only inline
            # in POST /v1/road/workflow-runs/{run_id}/resume-node -- there
            # is no service verb to call here, and copying that route's body
            # into a second place is exactly the drift this vertical avoids.
            await websocket.send_json({"event": "command_error", "id": cmd_id,
                                       "code": "use_http_resume",
                                       "message": "use POST resume-node"})
            continue
        if not isinstance(node_id, str) or not node_id:
            await websocket.send_json({"event": "command_error", "id": cmd_id,
                                       "code": "bad_request",
                                       "message": "node_id must be a non-empty string"})
            continue

        event, payload = _run_command(cmd, tenant_id=tenant_id, run_id=run_id,
                                      node_id=node_id, user_id=user_id, args=args)
        if event == "forbidden":
            # Authorization lost mid-socket: one error frame, then close with
            # the same code the handshake refused with.
            await websocket.send_json({"type": "error", "code": "forbidden"})
            await websocket.close(code=CLOSE_FORBIDDEN, reason="authorization lost")
            return
        await websocket.send_json({"event": event, "id": cmd_id, **payload})


async def publish_debug_event(run_id, event: dict):
    """Seam for sz-2.5: orchestrator -> socket push of `breakpoint_hit`.

    Out of scope for sz-2.4 (the orchestrator owns the push); this is the hook
    it will hang on, so the channel's shape is fixed here and not invented
    later. No-op today.
    """
    return None
