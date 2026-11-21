"""sz-4.5b — calendar trigger via the OAuth-broker pull (Google Calendar +
Microsoft 365), poll-style.

A POLL trigger whose configuration carries `{"source": "calendar", ...}` is
evaluated here instead of the http/rss pollers in streaming.py (those branch on
`kind`, so a calendar row never reaches them). Everything else — state
persistence, dedupe, rate limiting, dispatch — is the same pipeline the other
triggers use.

Provider HTTP is always to a static provider endpoint (googleapis.com /
graph.microsoft.com), never a customer URL, so — exactly like services/oauth_broker
— no egress-policy round trip is needed; the URL path component is the validated,
url-encoded calendar id only.

Nothing here raises out of a tick: a dead/foreign/revoked connection or a
provider 4xx marks the trigger degraded in its poll state with a REASON CODE
only (never a provider body, never a token) and the tick moves on.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from urllib.parse import quote

from services import oauth_broker
from services.triggers import pipeline

from product_names import themed_logger

logger = themed_logger(__name__, 'triggers')

GOOGLE = "google"
MICROSOFT365 = "microsoft365"
_PROVIDERS = (GOOGLE, MICROSOFT365)
_EVENTS = ("created", "updated", "starting_soon")

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024   # stream cap, per page
_HTTP_TIMEOUT_S = 20
_MAX_PAGES = 10                        # pagination cap, per tick per trigger
_SEEN_KEEP = 2000                      # bounded seen-set
_SUMMARY_MAX = 500
_ATTENDEE_MAX = 50
_DEFAULT_INTERVAL_S = 300
_REAUTH_BACKOFF_S = 900                # one retry window, not a retry storm
_OVERSIZE = "response_too_large"
_SKIPPED_STATUSES = ("cancelled", "canceled", "declined")

# Tests inject an httpx.MockTransport here; production stays None.
_TEST_TRANSPORT: Optional[httpx.BaseTransport] = None


def _http_client_factory() -> httpx.Client:
    """Seam so tests hand in a MockTransport; production gets a plain
    trust-env-off client (provider endpoints are ours, not the customer's
    URL, so no egress-policy round trip is needed here)."""
    return httpx.Client(trust_env=False, timeout=_HTTP_TIMEOUT_S,
                        follow_redirects=False, transport=_TEST_TRANSPORT)


# --- config validation -------------------------------------------------------


def validate_calendar_config(cfg: Any) -> Dict[str, Any]:
    """Strict validator for a calendar trigger's configuration JSON.

    Returns a normalized dict (defaults filled); raises ValueError on anything
    else. `calendar_id` is "primary" or an opaque provider id: <=255 chars,
    never containing "/" or ".." (it becomes a URL path component, and it is
    url-encoded when used, so a traversal like "../x" is refused twice over).
    """
    if not isinstance(cfg, dict):
        raise ValueError("configuration must be an object")
    if cfg.get("source") != "calendar":
        raise ValueError("source must be 'calendar'")

    cid = cfg.get("connection_id")
    if isinstance(cid, bool) or not isinstance(cid, int) or cid <= 0:
        raise ValueError("connection_id must be a positive integer")

    cal = cfg.get("calendar_id", "primary")
    if not isinstance(cal, str) or not cal or len(cal) > 255:
        raise ValueError("calendar_id must be a non-empty string of <=255 chars")
    if "/" in cal or ".." in cal or any(ord(ch) < 32 or ord(ch) == 127
                                        for ch in cal):
        raise ValueError("calendar_id must not contain '/' or '..'")

    event = cfg.get("event")
    if event not in _EVENTS:
        raise ValueError("event must be one of created|updated|starting_soon")

    lead = cfg.get("lead_minutes")
    if event == "starting_soon":
        if isinstance(lead, bool) or not isinstance(lead, int) or not 1 <= lead <= 1440:
            raise ValueError("lead_minutes must be an integer 1..1440")
    elif lead is not None:
        raise ValueError("lead_minutes is only valid for event='starting_soon'")

    interval = cfg.get("interval_seconds", _DEFAULT_INTERVAL_S)
    if isinstance(interval, bool) or not isinstance(interval, int) or not 0 <= interval <= 86400:
        raise ValueError("interval_seconds must be an integer 0..86400")

    unknown = set(cfg) - {"source", "connection_id", "calendar_id", "event",
                          "lead_minutes", "interval_seconds"}
    if unknown:
        raise ValueError(f"unknown configuration key(s): {sorted(unknown)}")

    return {"source": "calendar", "connection_id": cid, "calendar_id": cal,
            "event": event, "lead_minutes": lead, "interval_seconds": interval}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _state_row(db, trig):
    """The trigger's poll-state row (same TriggerEventLog row the http/rss
    pollers use — a calendar trigger owns its own row, keyed 'poll-state')."""
    from models import TriggerEventLog
    row = (db.query(TriggerEventLog)
           .filter(TriggerEventLog.trigger_id == trig.id,
                   TriggerEventLog.event_key == "poll-state").first())
    if row is None:
        row = TriggerEventLog(trigger_id=trig.id, tenant_id=trig.tenant_id,
                              event_key="poll-state", state={})
        db.add(row)
        db.commit()
    return row


def _mark_degraded(db, trig, row, code: str) -> None:
    """Reason CODE only — never a provider body, never a token."""
    st = dict(row.state or {})
    st["degraded"] = code
    st["degraded_at"] = _now().isoformat()
    row.state = st
    db.commit()
    logger.warning("calendar trigger %s degraded (%s) — no dispatch", trig.id, code)


def _connection(db, trig, cid: int):
    """(conn, None) or (None, reason_code). Tenant-scoped query: a foreign
    connection id is simply not found, so no cross-tenant data is read."""
    from models import OAuthConnection
    conn = (db.query(OAuthConnection)
            .filter(OAuthConnection.id == cid,
                    OAuthConnection.tenant_id == trig.tenant_id,
                    OAuthConnection.deleted_at.is_(None)).one_or_none())
    if conn is None:
        return None, "connection_missing"
    if conn.provider not in _PROVIDERS:
        return None, "provider_unsupported"
    if conn.status != "active":
        return None, "reauth_required"
    return conn, None


def _get_json(client: httpx.Client, url: str, headers: Dict[str, str]):
    """GET + stream with a hard 2 MiB cap. Returns (payload, error_code).

    401 -> 'reauth_required' (the caller backs off, it does not retry-storm);
    410 -> 'gone' (Google's "sync token expired" signal). Bodies are never
    logged and never returned upward on error.
    """
    try:
        with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 401:
                return None, "reauth_required"
            if resp.status_code == 410:
                return None, "gone"
            if resp.status_code >= 400:
                return None, "http_error"
            total = 0
            chunks: List[bytes] = []
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > _MAX_RESPONSE_BYTES:
                    return None, _OVERSIZE
                chunks.append(chunk)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        logger.warning("calendar fetch failed (transport): %s", type(exc).__name__)
        return None, "http_error"
    try:
        return json.loads(b"".join(chunks).decode("utf-8")), None
    except (UnicodeError, ValueError):
        return None, "bad_json"


def _google_pages(client, calendar_id: str, access_token: str,
                  sync_token: Optional[str]):
    """(items, next_sync_token, error_code); at most _MAX_PAGES requests.

    Authorization always carries the OAuth access token; `syncToken` is a
    query param (Google's incremental-sync contract), sent only on the first
    page of a sync — subsequent pages use `pageToken` alone.
    """
    from urllib.parse import urlencode
    base = (f"https://www.googleapis.com/calendar/v3/calendars"
            f"/{quote(calendar_id, safe='')}/events")
    items: List[dict] = []
    page_token: Optional[str] = None
    headers = {"Authorization": f"Bearer {access_token}"}
    for _ in range(_MAX_PAGES):
        params = {"singleEvents": "true", "maxResults": "250"}
        if page_token:
            params["pageToken"] = page_token
        elif sync_token:
            params["syncToken"] = sync_token
        payload, err = _get_json(client, f"{base}?{urlencode(params)}", headers)
        if err:
            return items, None, err
        items.extend(i for i in (payload.get("items") or []) if isinstance(i, dict))
        page_token = payload.get("nextPageToken")
        if not page_token:
            return items, payload.get("nextSyncToken"), None
    return items, None, "pagination_capped"


_MS_HOST = "graph.microsoft.com"


def _valid_ms_url(url: Any) -> bool:
    """A reused/provider-returned Microsoft Graph link must stay on
    graph.microsoft.com over https — refuse anything else (SSRF: a stored or
    provider-returned nextLink/deltaLink is untrusted input)."""
    if not isinstance(url, str) or not url:
        return False
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == _MS_HOST


def _ms_base(calendar_id: str) -> str:
    return (f"https://graph.microsoft.com/v1.0/me/calendars"
            f"/{quote(calendar_id, safe='')}/events/delta")


def _microsoft_pages(client, calendar_id: str, access_token: str,
                     delta_link: Optional[str]):
    """(items, delta_link, error_code); at most _MAX_PAGES requests.

    Authorization always carries the OAuth access token. A reused delta_link
    (from prior poll state) or a provider-returned nextLink/deltaLink must
    resolve to graph.microsoft.com or it is refused rather than followed.
    """
    base = _ms_base(calendar_id)
    url = delta_link if _valid_ms_url(delta_link) else base
    items: List[dict] = []
    final_link: Optional[str] = None
    headers = {"Authorization": f"Bearer {access_token}"}
    for _ in range(_MAX_PAGES):
        payload, err = _get_json(client, url, headers)
        if err:
            return items, final_link, err
        items.extend(i for i in (payload.get("value") or []) if isinstance(i, dict))
        delta = payload.get("@odata.deltaLink") or payload.get("deltaLink")
        if delta:
            final_link = delta
        nxt = payload.get("@odata.nextLink") or payload.get("nextLink")
        if not nxt:
            return items, final_link, None
        if not _valid_ms_url(nxt):
            logger.warning("calendar trigger: refused off-host Microsoft link")
            return items, final_link, "invalid_link"
        url = nxt
    return items, final_link, "pagination_capped"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _when(value: Any) -> Optional[str]:
    """Google/Microsoft start|end -> an ISO string (dateTime or date form)."""
    if isinstance(value, dict):
        for k in ("dateTime", "date", "startTime"):
            v = value.get(k)
            if isinstance(v, str) and v:
                return v
        return None
    return value if isinstance(value, str) else None


def _normalize(item: dict) -> Optional[Dict[str, Any]]:
    """Provider event -> the ONLY ctx payload shape (never the raw item: the
    raw item can carry attendee lists, reminders and provider metadata we have
    no reason to put in a workflow run's parameters)."""
    eid = item.get("id")
    if not isinstance(eid, str) or not eid or len(eid) > 1024:
        return None
    start_dt = _parse_dt(_when(item.get("start")))
    end_dt = _parse_dt(_when(item.get("end")))
    organizer = item.get("organizer")
    organizer_email = organizer.get("email") if isinstance(organizer, dict) else None
    attendees = [a.get("email") for a in item.get("attendees") or []
                 if isinstance(a, dict) and isinstance(a.get("email"), str)]
    summary = item.get("summary") or item.get("title") or ""
    return {
        "id": eid,
        "summary": str(summary)[:_SUMMARY_MAX],
        "start": _iso(start_dt),
        "end": _iso(end_dt),
        "organizer_email": (organizer_email if isinstance(organizer_email, str)
                            else None),
        "html_link": (item.get("htmlLink") if isinstance(item.get("htmlLink"), str)
                      else None),
        "status": (item.get("status") if isinstance(item.get("status"), str)
                   else None),
        "attendees": attendees[:_ATTENDEE_MAX],
    }


def _seen_key(event: Dict[str, Any], kind: str, start_iso: Optional[str]) -> str:
    """Fire once per (event id, kind[, start]) — a moved starting_soon event
    re-fires under its new start time; created/updated do not."""
    key = f"{event['id']}:{kind}"
    if kind == "starting_soon" and start_iso:
        key = f"{key}:{start_iso}"
    return key


def _fire(db, trig, st: Dict[str, Any], items: List[dict], kind: str,
          lead_minutes: Optional[int]) -> int:
    seen_list = list(st.get("seen") or [])
    seen = set(seen_list)
    now = _now()
    fired = 0
    for raw in items:
        event = _normalize(raw)
        if event is None:
            continue
        if (event["status"] or "").lower() in _SKIPPED_STATUSES:
            continue
        if kind == "starting_soon":
            if not event["start"]:
                continue
            delta = (_parse_dt(event["start"]) - now).total_seconds()
            if delta < 0 or delta > (lead_minutes or 0) * 60:
                continue
        key = _seen_key(event, kind, event["start"])
        if key in seen:
            continue
        seen.add(key)
        seen_list.append(key)
        payload = dict(event, event=kind)
        ctx = {"event_key": f"cal:{trig.id}:{key}"[:255],
               "calendar": payload,
               "inputs": {"calendar_event": payload}}
        if pipeline.evaluate_and_dispatch(db, trig, ctx).get("dispatched"):
            fired += 1
    st["seen"] = seen_list[-_SEEN_KEEP:]
    return fired


def _evaluate_one(db, trig) -> int:
    """One calendar trigger's tick. Never raises: every failure mode marks the
    trigger degraded (reason code) or logs and skips."""
    try:
        cfg = validate_calendar_config(trig.configuration or {})
    except ValueError as exc:
        logger.warning("calendar trigger %s config invalid: %s", trig.id, exc)
        return 0

    row = _state_row(db, trig)
    st = dict(row.state or {})
    now = _now()
    lp = st.get("last_polled")
    if lp:
        try:
            elapsed = (now - datetime.fromisoformat(lp)).total_seconds()
        except ValueError:
            elapsed = None
        if elapsed is not None and elapsed < cfg["interval_seconds"]:
            return 0

    conn, why = _connection(db, trig, cfg["connection_id"])
    if conn is None:
        _mark_degraded(db, trig, row, why)
        return 0

    if st.get("degraded") == "reauth_required":
        at = st.get("degraded_at")
        try:
            cooled = (now - datetime.fromisoformat(at)).total_seconds() >= _REAUTH_BACKOFF_S
        except (ValueError, TypeError):
            cooled = True
        if not cooled:
            return 0

    try:
        token = oauth_broker.get_access_token(db, conn)
    except oauth_broker.OAuthStateError:
        # reason code only — the broker's message may name the connection
        _mark_degraded(db, trig, row, "reauth_required")
        return 0

    st.pop("degraded", None)
    st.pop("degraded_at", None)
    provider = conn.provider
    sync = st.get("sync_token")
    client = _http_client_factory()
    try:
        if provider == GOOGLE:
            items, next_sync, err = _google_pages(
                client, cfg["calendar_id"], token, sync)
            if err == "gone":
                # sync token expired: full resync. Already-fired ids stay in
                # the seen-set, so a resync cannot re-fire them.
                items, next_sync, err = _google_pages(
                    client, cfg["calendar_id"], token, None)
        else:
            delta = st.get("delta_link")
            items, next_sync, err = _microsoft_pages(
                client, cfg["calendar_id"], token, delta)
    finally:
        client.close()

    if err == _OVERSIZE:
        st["degraded"] = _OVERSIZE
        st["degraded_at"] = now.isoformat()
        logger.warning("calendar trigger %s response exceeded %d bytes — refused",
                       trig.id, _MAX_RESPONSE_BYTES)
        row.state = st
        db.commit()
        return 0
    if err == "reauth_required":
        _mark_degraded(db, trig, row, "reauth_required")
        return 0
    if err:
        logger.warning("calendar trigger %s fetch failed: %s", trig.id, err)
        st["last_error"] = err
        st["last_polled"] = now.isoformat()
        row.state = st
        db.commit()
        return 0

    fired = _fire(db, trig, st, items, cfg["event"], cfg["lead_minutes"])
    st["last_polled"] = now.isoformat()
    if provider == GOOGLE:
        if next_sync:
            st["sync_token"] = next_sync
    else:
        if next_sync:
            st["delta_link"] = next_sync
    row.state = st
    db.commit()
    return fired


def evaluate_calendar_triggers(db) -> int:
    """Poll due calendar triggers; fire on new/changed events. Returns count
    dispatched. Each trigger runs in its own SAVEPOINT: one bad row cannot
    poison the rest of the tick."""
    from models import WorkflowTrigger, WorkflowTriggerType
    fired = 0
    triggers = (db.query(WorkflowTrigger).filter(
        WorkflowTrigger.trigger_type == WorkflowTriggerType.POLL,
        WorkflowTrigger.is_active.is_(True),
        WorkflowTrigger.deleted_at.is_(None)).all())
    for trig in triggers:
        if (trig.configuration or {}).get("source") != "calendar":
            continue
        try:
            fired += _evaluate_one(db, trig)
        except Exception:  # noqa: BLE001 — terminal: never raise out of the tick
            logger.exception("calendar trigger %s evaluation failed", trig.id)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
    return fired
