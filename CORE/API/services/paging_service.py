"""GSEC FX.119 p2a (owner decision Q106=A) — the API-side on-call paging client.

One page = one POST to the self-hosted ntfy push stack (deploy/ntfy/docker-compose.ntfy.example.yml):
the on-call person's phone runs the ntfy client app and subscribes to the topic; the API
publishes to it. Nothing third-party is involved — no PagerDuty, no vendor register row, and
with `upstream-base-url` unset on the stack the page never leaves MOD's own origin, so it still
reaches the device with the internet cut (the same local-DNS + CA-trust path as
CORE/startup/OFFLINE_ACCESS.md).

Contract (this is a notification SIDE EFFECT, never a decision):
  * disabled / unreachable ntfy NEVER raises into the caller — a result dict is returned and a
    WARNING logged. A paging failure must not break the monitor that detected the incident.
  * every page SENT and every page FAILED is audited (append-only PermissionAuditLog rows,
    ONCALL_PAGE_SENT / ONCALL_PAGE_FAILED — ISO 27001 AU-2/AU-3, no hard deletes).
  * identical `dedupe_key` values inside oncall.push_dedupe_window_seconds send ONE page; the
    duplicate is reported `deduped` and never re-POSTed (a storm of the same monitor alert
    cannot flood the phone or the audit trail).
  * the publisher token comes from Vault (KV path oncall.push_vault_path, field
    `publisher_token`) — the same two auth paths the rest of the API uses (services/key_provider.py
    `_vault_token_for_transit`), and the token value is NEVER logged, never in a setting, never in
    audit metadata.

Wiring the monitors and the Priority Sev-1 ticket to this helper is FX.119 p2b. The ROTA
(oncall.rota / oncall.timezone -> current_on_call()), the ACK link and the 15-minute
escalation (oncall.ack_window_seconds / oncall.owner_topic -> models.OncallPage +
services/oncall_ack.py + the fx119_oncall_escalation scheduler tick) are FX.119 p3a/p3b
and ARE read here: send_page() now publishes to the topic of whoever is on call right
now (falling back to oncall.owner_topic, then to oncall.push_topic), records the page
as a models.OncallPage so it can be acked, and attaches the ack link to the page.
"""
from __future__ import annotations

import zlib
import hashlib
import logging
import secrets as _secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import models

from services import platform_settings as _psr

from utils.vault_client import read_kv_v2

logger = logging.getLogger(__name__)

_SENT_ACTION = models.AuditAction.ONCALL_PAGE_SENT
_FAILED_ACTION = models.AuditAction.ONCALL_PAGE_FAILED
_RESOURCE_TYPE = "oncall_page"

# ntfy accepts priority 1 (low) .. 10 (urgent); anything outside is clamped, never sent as-is.
_PRIORITY_MIN, _PRIORITY_MAX = 1, 10


def _setting(key: str, db=None) -> Any:
    """One settings read that degrades to the registered default on a read hiccup —
    a paging client must never die because the settings table hiccuped."""
    try:
        return _psr.get_setting(key, db=db)
    except Exception as exc:  # noqa: BLE001 — settings read must not break paging
        logger.warning("paging_service: settings read %s failed (%s); using default", key, exc)
        return _psr._spec(key).default


def _priority_for(priority: Any) -> int:
    """Map the caller's `priority` to an ntfy priority 1..10.

    Accepts an int already in range, or a severity label ('sev1', 'SEV-1', 'sev-1'), mapped
    through oncall.push_priority_map (keys compared case-insensitively, '-' stripped).
    Anything unmapped falls back to oncall.push_priority_default, then clamps into range.
    """
    default = _setting("oncall.push_priority_default")
    raw: Any = default
    if isinstance(priority, bool):
        raw = default
    elif isinstance(priority, int):
        raw = priority
    elif isinstance(priority, str):
        key = priority.strip().lower().replace("-", "").replace("_", "")
        table = _setting("oncall.push_priority_map") or {}
        norm = {str(k).strip().lower().replace("-", "").replace("_", ""): v
                for k, v in table.items() if isinstance(k, str)}
        raw = norm.get(key, default)
    try:
        val = int(raw)
    except (TypeError, ValueError):
        val = 5
    return max(_PRIORITY_MIN, min(_PRIORITY_MAX, val))


def _publisher_token() -> Optional[str]:
    """Vault-issued ntfy publisher token. Never logged, never returned to a caller."""
    try:
        from services.key_provider import _vault_token_for_transit
        vtok = _vault_token_for_transit()
        if not vtok:
            logger.warning("paging_service: no Vault agent/approle token available")
            return None
        path = str(_setting("oncall.push_vault_path") or "").strip("/")
        if not path:
            return None
        secret = read_kv_v2(path, token=vtok) or {}
        token = (secret.get("publisher_token") or "").strip()
        if not token:
            logger.warning("paging_service: no publisher_token at Vault path %s", path)
            return None
        return token
    except Exception as exc:  # noqa: BLE001 — a Vault outage is a failed page, not a crash
        logger.warning("paging_service: Vault publisher-token read failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# The ROTA (FX.119 p3b) — who is on call RIGHT NOW
# ---------------------------------------------------------------------------


def _rota(db) -> List[dict]:
    """The oncall.rota setting, defensively normalised. NEVER raises."""
    raw = _setting("oncall.rota", db=db)
    if not isinstance(raw, list):
        return []
    out: List[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        topic = str(entry.get("topic") or "").strip()
        if not name or not topic:
            continue
        days = entry.get("days", "all")
        if isinstance(days, str):
            days = [days]
        if not isinstance(days, list) or not days:
            days = ["all"]
        norm = {str(d).strip().lower() for d in days if isinstance(d, str)}
        out.append({
            "name": name,
            "topic": topic,
            "tier": (str(entry.get("tier") or "primary")).strip().lower(),
            "days": norm,
            "start": entry.get("start"),
            "end": entry.get("end"),
        })
    return out


def _entry_covers(entry: dict, local) -> bool:
    """True when this rota entry's weekly window covers `local` (a
    zone-localised datetime). start/end are 'HH:MM' in oncall.timezone;
    end < start wraps past midnight (a night shift 20:00 -> 06:00)."""
    days = entry.get("days") or {"all"}
    if "all" not in days:
        wd = local.strftime("%a").lower()[:3]
        if wd not in days:
            return False
    start, end = entry.get("start"), entry.get("end")
    if not start or not end:
        return True  # no window = on call around the clock

    def _mins(v: Any) -> Optional[int]:
        parts = str(v).strip().split(":")
        if len(parts) != 2:
            return None
        try:
            return int(parts[0]) * 60 + int(parts[1])
        except ValueError:
            return None

    lo, hi = _mins(start), _mins(end)
    if lo is None or hi is None:
        return True  # unparseable window: fail OPEN (the person is paged), never drop
    now_min = local.hour * 60 + local.minute
    if lo <= hi:
        return lo <= now_min < hi
    return now_min >= lo or now_min < hi  # wraps past midnight


def current_on_call(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The rota entry on call at `now` (default: right now). NEVER raises.

    Returns {'primary': entry|None, 'secondary': entry|None, 'topic': str|None,
    'rota_configured': bool, 'timezone': str}. Ties break by list order; the
    SECONDARY is the next matching entry of tier 'secondary' (the person the
    primary hands off to), so a page can name both without a second POST.
    """
    tz_name = str(_setting("oncall.timezone", db=db) or "UTC")
    when = now or datetime.now(tz=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        local = when.astimezone(ZoneInfo(tz_name))
    except Exception as exc:  # noqa: BLE001 — bad zone: UTC, and say so
        logger.warning("paging_service: oncall.timezone %s unresolvable (%s); "
                       "using UTC", tz_name, exc)
        tz_name, local = "UTC", when
    entries = _rota(db)
    primary = secondary = None
    for entry in entries:
        if not _entry_covers(entry, local):
            continue
        if entry["tier"] != "secondary" and primary is None:
            primary = entry
        elif entry["tier"] == "secondary" and secondary is None:
            secondary = entry
    return {
        "primary": primary, "secondary": secondary,
        "topic": (primary or secondary or {}).get("topic") or None,
        "rota_configured": bool(entries),
        "timezone": tz_name,
    }


# ---------------------------------------------------------------------------
# The audit trail (append-only; the page is a side effect beside the record)
# ---------------------------------------------------------------------------

def _page_resource_id(dedupe_key: Optional[str], topic: str) -> int:
    """Stable int for the audit row's `resource_id` (the column is an Integer).

    Same crc32 convention services/api_key_format.py uses. The dedupe read filters on
    this value, so two pages of the same key land on one dedupe bucket; with no key the
    bucket is the topic."""
    seed = dedupe_key if dedupe_key else topic
    # 31 bits, not 32: PermissionAuditLog.resource_id is a Postgres `integer`
    # (int32), so a full crc32 (up to 4294967295) would overflow the column.
    return zlib.crc32(str(seed).encode("utf-8")) & 0x7FFFFFFF


def _recent_page(db, tenant_id: Optional[int], resource_id: int, window_s: int) -> bool:
    """True when this dedupe bucket already SENT a page inside the window.

    Read off the audit table (append-only, so a previous page is never rewritten —
    ISO 27001), same shape as services/node_egress_approval._recent_block_event.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=window_s)
    q = db.query(models.PermissionAuditLog).filter(
        models.PermissionAuditLog.action == _SENT_ACTION,
        models.PermissionAuditLog.resource_type == _RESOURCE_TYPE,
        models.PermissionAuditLog.resource_id == resource_id,
        models.PermissionAuditLog.timestamp >= cutoff,
    )
    q = q.filter(models.PermissionAuditLog.tenant_id == (tenant_id or 0))
    return db.query(q.exists()).scalar() is True


def _audit_actor_user_id(db, tenant_id: Optional[int],
                         actor_user_id: Optional[int] = None) -> int:
    """BUG.B80b — the actor a paging audit row is attributed to.

    ``PermissionAuditLog.actor_user_id`` is a NOT-NULL FK to ``users.id``, so the
    literal ``0`` these emits passed FK-violates on a FK-enforcing DB (there is
    no user 0). Because the audit helper commits inside the CALLER's session,
    that failed INSERT left the Session rollback-required and the next DB access
    raised PendingRollbackError — the zombie BUG.B80 fixed in
    services/node_egress_approval.py (commit b282c0ed3).

    Paging has no login behind most emits (the monitor, the scheduler tick, the
    capability-URL ack), so the accountable party is the tenant's platform admin
    (the ``install_preflight_overrides._platform_admin_actor`` convention); an
    actor the caller already knows (the ack router, a request user) wins over
    it. With neither, the established system actor ``0`` is kept — harmless now
    because the write runs on a SAVEPOINT.

    Read-only and inside its own try: attribution can never move a paging
    outcome or raise out of the audit path.
    """
    try:
        if actor_user_id:
            return int(actor_user_id)
        if tenant_id:
            admin = (db.query(models.User)
                     .filter(models.User.tenant_id == tenant_id,
                             models.User.is_platform_admin.is_(True),
                             models.User.deleted_at.is_(None))
                     .first())
            if admin is not None:
                return int(admin.id)
    except Exception as exc:  # noqa: BLE001 — attribution must not break paging
        logger.warning("paging_service: audit actor lookup failed (using system "
                       "actor 0): %s", exc)
    return 0


def _write_audit_row(db, *, action, actor_user_id: int, tenant_id: Optional[int],
                     resource_id: int, metadata: Dict[str, Any]) -> Optional[int]:
    """One PermissionAuditLog row on its OWN savepoint. Never raises.

    BUG.B80b: the audit helper commits inside the CALLER's session, so a failed
    INSERT (FK miss on actor_user_id, IntegrityError, dead connection) used to
    abort that session and poison whoever paged. A savepoint rolls back ONLY the
    audit row, so the page outcome stands and the caller's session stays usable.
    Explicit savepoint object, not `with db.begin_nested():`, because
    audit.log_permission_audit commits internally (committing the savepoint
    itself) and a context-managed savepoint rejects anything after that inner
    commit — same shape as node_egress_approval.record_blocked_egress and
    asset_inventory._write_alert.
    """
    try:
        import audit  # local import: audit <-> services cycle guard, as elsewhere

        savepoint = db.begin_nested()
        try:
            row = audit.log_permission_audit(
                db=db,
                action=action,
                actor_user_id=actor_user_id,
                tenant_id=tenant_id or 0,
                resource_type=_RESOURCE_TYPE,
                resource_id=resource_id,
                metadata=metadata,
            )
            if row is None:
                raise RuntimeError("paging audit row not written")
            return row.id
        except Exception as exc:  # noqa: BLE001 — the SAVEPOINT rollback path
            try:
                savepoint.rollback()
            except Exception:  # already released by the audit write itself
                pass
            raise
    except Exception as exc:  # noqa: BLE001 — an audit hiccup must not break paging
        logger.warning("paging_service: audit write failed (paging outcome "
                       "unchanged, caller session intact): %s", exc)
        return None


def _audit_page(db, *, action, tenant_id: Optional[int], resource_id: int,
                title: str, topic: str, priority: int, reason: Optional[str] = None,
                status_code: Optional[int] = None, message: Optional[str] = None,
                actor_user_id: Optional[int] = None) -> Optional[int]:
    """Append one ONCALL_PAGE_SENT / ONCALL_PAGE_FAILED row. Never raises.

    Metadata carries identifiers and outcomes only — severity, topic, priority, the
    monitor's reason, the HTTP status — never the publisher token, never the full page
    body (the message is truncated; a page can quote an alert line, not a secret).

    BUG.B80b: the actor is a real user (caller-supplied actor, else the tenant's
    platform admin, else the system actor 0) and the write is savepoint-isolated.
    """
    return _write_audit_row(
        db,
        action=action,
        actor_user_id=_audit_actor_user_id(db, tenant_id, actor_user_id),
        tenant_id=tenant_id,
        resource_id=resource_id,
        metadata={
            "title": (title or "")[:200],
            "topic": topic,
            "priority": priority,
            "reason": reason,
            "status_code": status_code,
            "message_excerpt": (message or "")[:200],
        },
    )


# ---------------------------------------------------------------------------
# The PAGE RECORD (FX.119 p3a) — so the page can be ACKED and ESCALATED
# ---------------------------------------------------------------------------


def _ack_link(page_id: int) -> Optional[str]:
    """The ack URL the phone taps, or None when no origin is configured."""
    base = str(_setting("oncall.ack_base_url") or "").strip().rstrip("/")
    if not base:
        return None
    return f"{base}/v1/oncall/pages/{page_id}/ack"


def _new_ack_token() -> str:
    """A fresh single-use ack token. Only its hash is ever stored."""
    return _secrets.token_urlsafe(32)


def _hash_ack_token(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _record_page(db, *, title: str, priority: int, tenant_id: Optional[int],
                 topic: str, dedupe_key: Optional[str], ack_token: Optional[str],
                 status: str = "sent") -> Optional[int]:
    """Append the models.OncallPage row for this page. NEVER raises.

    The row is what makes the page ACKABLE: the ack token exists only here,
    hashed, and only while status == 'sent' (ck_oncall_pages_token_only_live).
    A FAILED page records status='failed' with NO token — there is nothing to
    ack on a phone that never buzzed, and the escalation sweep must not chase
    a page that never reached anyone.
    """
    try:
        row = models.OncallPage(
            title=(title or "")[:200] or "on-call page",
            priority=int(priority) if priority is not None else 5,
            tenant_id=tenant_id,
            sent_to=str(topic)[:128] if topic else None,
            sent_at=(datetime.now(tz=timezone.utc)
                     if status in ("sent", "failed") else None),
            dedupe_key=(str(dedupe_key)[:128] if dedupe_key else None),
            ack_token_hash=_hash_ack_token(ack_token) if ack_token else None,
            status=status,
        )
        db.add(row)
        db.flush()
        return row.id
    except Exception as exc:  # noqa: BLE001 — a record hiccup must not break paging
        logger.warning("paging_service: OncallPage record write failed: %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def _mark_page_failed(db, page_id: Optional[int]) -> None:
    """Flip a recorded page to 'failed' and CONSUME its ack token.

    A page that never reached a phone has nothing to ack: leaving a live
    token on it would let a link nobody was handed decide an incident that
    was never delivered. NEVER raises.
    """
    if not page_id:
        return
    try:
        row = (db.query(models.OncallPage)
               .filter(models.OncallPage.id == page_id).one_or_none())
        if row is None:
            return
        row.status = "failed"
        row.ack_token_hash = None
        db.flush()
    except Exception as exc:  # noqa: BLE001 — a record hiccup must not break paging
        logger.warning("paging_service: failed-page mark write failed: %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _audit_no_rota(db, *, tenant_id: Optional[int], topic: str) -> None:
    """One ONCALL_NO_ROTA row per window (not per page).

    The compliance trail must SHOW that a page went to the owner topic because
    no rota is configured — an unacknowledged gap in 'who was told' is exactly
    what an auditor asks. Deduped off the audit table the same way pages are,
    inside oncall.push_dedupe_window_seconds, so a storm of monitors cannot
    flood the trail with the same fact.
    """
    window = int(_setting("oncall.push_dedupe_window_seconds", db=db) or 0)
    rid = _page_resource_id(f"no-rota:{topic}", topic)
    if window > 0:
        try:
            cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=window)
            q = db.query(models.PermissionAuditLog).filter(
                models.PermissionAuditLog.action == models.AuditAction.ONCALL_NO_ROTA,
                models.PermissionAuditLog.resource_type == _RESOURCE_TYPE,
                models.PermissionAuditLog.resource_id == rid,
                models.PermissionAuditLog.timestamp >= cutoff,
            )
            q = q.filter(models.PermissionAuditLog.tenant_id == (tenant_id or 0))
            if db.query(q.exists()).scalar() is True:
                return
        except Exception as exc:  # noqa: BLE001 — a failed read means "emit"
            logger.warning("paging_service: no-rota dedupe read failed: %s", exc)
    # BUG.B80b: real actor + savepoint-isolated write (see _audit_actor_user_id /
    # _write_audit_row) — the old literal 0 FK-violated against users.id and the
    # audit helper commits inside the caller's session, poisoning it.
    _write_audit_row(
        db,
        action=models.AuditAction.ONCALL_NO_ROTA,
        actor_user_id=_audit_actor_user_id(db, tenant_id),
        tenant_id=tenant_id,
        resource_id=rid,
        metadata={
            "reason": "no rota configured; page fell back to the owner topic",
            "topic": topic,
        },
    )


def send_page(db, *, title: str, message: str, priority: Any = None,
              tags: Optional[List[str]] = None, click_url: Optional[str] = None,
              dedupe_key: Optional[str] = None,
              tenant_id: Optional[int] = None,
              ack_token: Optional[str] = None,
              record: bool = True) -> Dict[str, Any]:
    """Send one on-call page to the self-hosted push stack. NEVER raises.

    Returns {'sent', 'reason', 'deduped', 'audit_id', 'status_code', 'topic',
    'priority'} plus the p3a keys 'page_id' (the models.OncallPage row, so the
    caller can ack/escalate it) and 'ack_url' (the link the PHONE taps — the
    token itself is only ever in that URL, hashed in the record).

    p3b routing: the page goes to the topic of whoever is ON CALL right now
    (oncall.rota + oncall.timezone); with no rota entry covering NOW it falls
    back to oncall.owner_topic — audited ONCALL_NO_ROTA once per window — and
    then to oncall.push_topic. Only optional kwargs were added, so every p2a
    caller keeps working unchanged.
    """
    out: Dict[str, Any] = {
        "sent": False, "reason": None, "deduped": False, "audit_id": None,
        "status_code": None, "topic": None, "priority": None,
        "page_id": None, "ack_url": None,
    }
    base, topic = "", ""
    try:
        enabled = bool(_setting("oncall.paging_enabled", db=db))
        base = str(_setting("oncall.push_base_url", db=db) or "").strip()
        default_topic = str(_setting("oncall.push_topic", db=db) or "").strip()
        owner_topic = str(_setting("oncall.owner_topic", db=db) or "").strip()
        out["priority"] = _priority_for(priority)
        if not enabled:
            out["reason"] = "oncall.paging_enabled=false"
            return out
        # p3b ROUTING (only once paging is ENABLED, so a disabled channel
        # still makes no HTTP call AND writes no rota audit row): the page
        # goes to whoever is on call NOW; with no rota entry covering the
        # moment it falls back to oncall.owner_topic — audited ONCALL_NO_ROTA
        # once per window — and then to the platform default topic.
        rota = current_on_call(db, None)
        topic = (rota.get("topic") or "").strip()
        if not rota["rota_configured"] or not topic:
            topic = owner_topic or default_topic
            if owner_topic:
                _audit_no_rota(db, tenant_id=tenant_id, topic=owner_topic)
        out["topic"] = topic or None
        if not base or not topic:
            out["reason"] = "no push channel configured"
            out["audit_id"] = _audit_page(
                db, action=_FAILED_ACTION, tenant_id=tenant_id,
                resource_id=_page_resource_id(dedupe_key, topic or "-"),
                title=title, topic=topic or "-", priority=out["priority"],
                reason=out["reason"])
            logger.warning("paging_service: page NOT sent, no push channel configured")
            return out

        resource_id = _page_resource_id(dedupe_key, topic)
        window = int(_setting("oncall.push_dedupe_window_seconds", db=db) or 0)
        if window > 0:
            try:
                if _recent_page(db, tenant_id, resource_id, window):
                    out["deduped"] = True
                    out["reason"] = "deduped within %ss" % window
                    logger.info(
                        "paging_service: page DEDUPED (key=%s topic=%s within %ss)",
                        dedupe_key or topic, topic, window)
                    return out
            except Exception as exc:  # noqa: BLE001 — a failed read means "emit anyway"
                logger.warning("paging_service: dedupe read failed (emitting): %s", exc)

        token = _publisher_token()
        if not token:
            out["reason"] = "no Vault publisher token"
            out["audit_id"] = _audit_page(
                db, action=_FAILED_ACTION, tenant_id=tenant_id, resource_id=resource_id,
                title=title, topic=topic, priority=out["priority"], reason=out["reason"])
            return out

        headers = {
            "Authorization": f"Bearer {token}",
            "Title": title or "",
            "X-Priority": str(out["priority"]),
            "X-Tags": ",".join(str(t) for t in (tags or [])),
        }
        # p3a: the page is a RECORD before it is a POST, because the ack link
        # the phone taps carries the record's id. The token is handed out in
        # the URL exactly once; only its hash is stored.
        live_token = ack_token or _new_ack_token()
        if record:
            out["page_id"] = _record_page(
                db, title=title, priority=out["priority"], tenant_id=tenant_id,
                topic=topic, dedupe_key=dedupe_key,
                ack_token=live_token)
        if out["page_id"]:
            link = _ack_link(out["page_id"])
            if link:
                ack_url = f"{link}?token={live_token}"
                out["ack_url"] = ack_url
                headers["X-Click"] = ack_url
                # ntfy's own Actions header: the phone's app opens this link
                # on tap, so the ack needs no browser and no copy-paste.
                headers["X-Actions"] = (
                    f'[{{"action":"open","url":"{ack_url}","title":"ACK"}}]'
                )
        if click_url and not out["ack_url"]:
            headers["X-Click"] = click_url
        timeout = max(1, int(_setting("oncall.push_timeout_seconds", db=db) or 5))
        import httpx
        resp = httpx.post(f"{base.rstrip('/')}/{topic}", content=message or "",
                          headers=headers, timeout=timeout)
        out["status_code"] = int(getattr(resp, "status_code", 0) or 0)
        if out["status_code"] < 400:
            out["sent"] = True
            out["audit_id"] = _audit_page(
                db, action=_SENT_ACTION, tenant_id=tenant_id, resource_id=resource_id,
                title=title, topic=topic, priority=out["priority"], message=message)
            return out
        out["reason"] = f"ntfy HTTP {out['status_code']}"
        out["audit_id"] = _audit_page(
            db, action=_FAILED_ACTION, tenant_id=tenant_id, resource_id=resource_id,
            title=title, topic=topic, priority=out["priority"], reason=out["reason"],
            status_code=out["status_code"])
        _mark_page_failed(db, out["page_id"])
        logger.warning("paging_service: page FAILED (ntfy HTTP %s)", out["status_code"])
        return out
    except Exception as exc:  # noqa: BLE001 — paging must never raise into the caller
        logger.warning("paging_service: page FAILED (%s)", exc)
        out["reason"] = out["reason"] or f"{type(exc).__name__}: {exc}"[:200]
        # A timeout / connection error / settings blow-up is a FAILED page too:
        # the audit trail must show the attempt, not just the log line.
        if not out["deduped"]:
            try:
                out["audit_id"] = _audit_page(
                    db, action=_FAILED_ACTION, tenant_id=tenant_id,
                    resource_id=_page_resource_id(dedupe_key, topic or "-"),
                    title=title, topic=topic or "-", priority=out["priority"] or 0,
                    reason=out["reason"])
            except Exception:  # noqa: BLE001 — the audit write already swallows
                pass
        # The record must agree with the audit: a page that timed out reached
        # nobody, so it is 'failed' and its ack token is consumed.
        _mark_page_failed(db, out["page_id"])
        return out


# ---------------------------------------------------------------------------
# ACK (FX.119 p3a) — the on-call person tapped the link on their phone
# ---------------------------------------------------------------------------


def ack_page(db, *, page_id: int, token: Optional[str],
             actor_user_id: int = 0) -> Dict[str, Any]:
    """Consume a page's single-use ack token. NEVER raises.

    Returns {'acked', 'reason', 'page_id', 'audit_id'}. The token is compared
    against the STORED SHA-256 hash with hmac.compare_digest (constant time:
    an ack link on a phone is a guessable-surface oracle otherwise), and the
    ack is legal only while the page is 'sent' — a replayed link is refused,
    so an ack can never be stamped twice and a lapsed page cannot be acked
    back to life after the owner was already paged.
    """
    import hmac as _hmac

    out: Dict[str, Any] = {"acked": False, "reason": None,
                           "page_id": page_id, "audit_id": None}
    try:
        row = (db.query(models.OncallPage)
               .filter(models.OncallPage.id == page_id,
                       models.OncallPage.deleted_at.is_(None)).one_or_none())
        if row is None:
            out["reason"] = "unknown_page"
            return out
        if row.status != "sent":
            out["reason"] = "already_decided"
            return out
        if not row.ack_token_hash:
            out["reason"] = "no_ack_token"
            return out
        if not token:
            out["reason"] = "missing_token"
            return out
        if not _hmac.compare_digest(_hash_ack_token(token), row.ack_token_hash):
            out["reason"] = "bad_token"
            return out
        row.acked_at = datetime.now(tz=timezone.utc)
        row.acked_by = int(actor_user_id or 0)
        row.status = "acked"
        # The token is CONSUMED: a replayed link has nothing to match.
        row.ack_token_hash = None
        db.flush()
        out["acked"] = True
        out["audit_id"] = _audit_event(
            db, models.AuditAction.ONCALL_PAGE_ACKED, row,
            extra={"ack_source": "token"})
        return out
    except Exception as exc:  # noqa: BLE001 — an ack hiccup must not raise
        logger.warning("paging_service: ack_page failed: %s", exc)
        out["reason"] = out["reason"] or f"{type(exc).__name__}: {exc}"[:200]
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return out


def _audit_event(db, action, row, extra: Optional[dict] = None,
                 reason: Optional[str] = None) -> Optional[int]:
    """One audit row ABOUT an OncallPage (ack / escalation). NEVER raises.

    BUG.B80b: the actor is the page's own `acked_by` when the row carries one
    (the person who tapped the link is the real actor for an ACK row), else the
    tenant's platform admin, else the system actor 0 — and the write is
    savepoint-isolated so a failed emit cannot poison the caller's session.
    """
    try:
        meta = {
            "page_id": row.id,
            "title": (row.title or "")[:200],
            "priority": row.priority,
            "topic": row.sent_to,
            "status": row.status,
            "dedupe_key": row.dedupe_key,
        }
        if extra:
            meta.update(extra)
        if reason:
            meta["reason"] = reason
        return _write_audit_row(
            db,
            action=action,
            actor_user_id=_audit_actor_user_id(db, row.tenant_id,
                                               getattr(row, "acked_by", None)),
            tenant_id=row.tenant_id,
            resource_id=_page_resource_id(str(row.id), _RESOURCE_TYPE),
            metadata=meta,
        )
    except Exception as exc:  # noqa: BLE001 — an audit hiccup must not break the ack
        logger.warning("paging_service: page-event audit write failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# ESCALATION (FX.119 p3a) — nobody acked in time, page the owner ONCE
# ---------------------------------------------------------------------------


def _escalate_page(db, row) -> bool:
    """Re-page ONE lapsed page to oncall.owner_topic. NEVER raises.

    Deliberately NOT routed through send_page: the escalation is a different
    fact ("nobody acked this") and must not be swallowed by the original
    page's dedupe bucket, nor create a second OncallPage row (the row IS the
    incident; it just changes state). One hop only — an escalation is never
    itself escalated, so two phones can never ping-pong.
    """
    try:
        owner = str(_setting("oncall.owner_topic", db=db) or "").strip()
        base = str(_setting("oncall.push_base_url", db=db) or "").strip()
        if not owner or not base:
            logger.warning(
                "paging_service: escalation for page %s has no owner topic/base "
                "configured (escalation target missing)", row.id)
            return False
        token = _publisher_token()
        if not token:
            logger.warning("paging_service: escalation for page %s has no "
                           "publisher token", row.id)
            return False
        ack_link = _ack_link(row.id)
        headers = {
            "Authorization": f"Bearer {token}",
            "Title": f"ESCALATION: {row.title or 'on-call page'}",
            "X-Priority": "10",
            "X-Tags": "urgent,escalation",
        }
        if ack_link:
            headers["X-Click"] = ack_link
        timeout = max(1, int(_setting("oncall.push_timeout_seconds", db=db) or 5))
        body = (f"UNACKED on-call page (page_id={row.id}, "
                f"was on {row.sent_to or '-'}): {row.title or ''}")
        import httpx
        resp = httpx.post(f"{base.rstrip('/')}/{owner}", content=body,
                          headers=headers, timeout=timeout)
        code = int(getattr(resp, "status_code", 0) or 0)
        if code >= 400:
            logger.warning("paging_service: escalation POST for page %s got "
                           "HTTP %s (stays 'sent', the next tick retries)",
                           row.id, code)
            return False
        row.escalated_at = datetime.now(tz=timezone.utc)
        row.escalated_to = owner[:128]
        row.status = "escalated"
        # The ack window is CLOSED: the on-call person's link can no longer
        # decide this page (ck_oncall_pages_token_only_live enforces it too).
        row.ack_token_hash = None
        db.flush()
        _audit_event(db, models.AuditAction.ONCALL_PAGE_ESCALATED, row,
                     extra={"escalated_to": owner})
        return True
    except Exception as exc:  # noqa: BLE001 — an unreachable owner must not raise
        logger.warning("paging_service: escalation for page %s failed: %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def escalate_due_pages(db) -> Dict[str, int]:
    """One escalation sweep (the scheduler tick's workhorse). NEVER raises.

    A page is due when it is still 'sent' (never acked, never escalated) and
    its ack window closed: sent_at + oncall.ack_window_seconds <= now. Each
    due page escalates EXACTLY ONCE — the status flip to 'escalated' is what
    makes the next sweep see nothing, so a lapsed page cannot be re-paged
    every minute. An acked page is invisible to this sweep by construction.
    """
    counters = {"due": 0, "escalated": 0}
    try:
        window = int(_setting("oncall.ack_window_seconds", db=db) or 900)
    except Exception:  # noqa: BLE001 — settings hiccup: the default window
        window = 900
    try:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=window)
        rows = (db.query(models.OncallPage)
                .filter(models.OncallPage.status == "sent",
                        models.OncallPage.sent_at.isnot(None),
                        models.OncallPage.sent_at <= cutoff,
                        models.OncallPage.deleted_at.is_(None)).all())
    except Exception as exc:  # noqa: BLE001 — the sweep read itself
        logger.warning("paging_service: escalation sweep read failed: %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return counters
    for row in rows:
        counters["due"] += 1
        if _escalate_page(db, row):
            counters["escalated"] += 1
    return counters
