"""MOD Assistant — action vocabulary + server-side validator
(mod-assistant-agent §6).

This is the single grounding gate between what the model emits and
what reaches the SSE stream (and ultimately the client). Three
layers of defence:

  1. Vocabulary check — the action name must be in `ACTION_VOCABULARY`.
  2. Per-tenant allowlist — the tenant admin can disable specific
     actions (e.g. open-new-tab) via `AssistantTenantConfig.action_allowlist`.
     The persisted shape stores ONLY the deviations: missing keys
     are interpreted as "allowed", a `false` value disables it.
  3. Site-map grounding — `navigate` / `open-new-tab` URLs must
     exist in the parsed site map; `open-dialog` ids must be
     present on at least one page. Hallucinated URLs and dialog
     ids are dropped before they stream.

`chain` actions recurse — every child gets the same three-layer
treatment, and a chain whose children all get dropped is itself
dropped (a chain with zero steps is not useful).

Public API:
  ACTION_VOCABULARY              — set of valid action names
  validate_action(action, site_map, allowlist=None) -> Optional[dict]
      Returns the normalized action dict on accept, None on reject.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from utils import assistant_site_map

logger = logging.getLogger(__name__)


# Single source of truth for what the model can emit.
ACTION_VOCABULARY = frozenset({
    "navigate",
    "open-new-tab",
    "open-dialog",
    "open-tab",
    "scroll-to",
    "highlight",
    "chain",
    "support-contacted",
})


def _is_action_allowed(name: str, allowlist: Optional[Dict[str, Any]]) -> bool:
    """Per-tenant allowlist semantics: missing keys = allowed,
    explicit `false` = disabled. Truthy values default to allowed.
    """
    if not allowlist:
        return True
    if name not in allowlist:
        return True
    return bool(allowlist[name])


def _validate_navigate(
    action: Dict[str, Any], site_map: assistant_site_map.SiteMap,
) -> Optional[Dict[str, Any]]:
    url = action.get("url")
    if not isinstance(url, str) or not url:
        return None
    page = site_map.page_for_url(url)
    if page is None:
        logger.info("assistant: dropping unmapped navigate url=%r", url)
        return None
    # `app` lets the client resolve cross-subdomain navigations
    # (Dashboard → WorkflowBuilder etc.) without mirroring the site
    # map on the client.
    # `label` is the destination page's display_name so the button
    # reads "Take me to Billing" instead of a generic
    # "Take me to the Dashboard" — two navigates with different
    # URLs but the same app would otherwise render identically.
    return {
        "action": "navigate",
        "url": url,
        "app": page.app,
        "label": page.display_name,
    }


def _validate_open_new_tab(
    action: Dict[str, Any], site_map: assistant_site_map.SiteMap,
) -> Optional[Dict[str, Any]]:
    url = action.get("url")
    if not isinstance(url, str) or not url:
        return None
    page = site_map.page_for_url(url)
    if page is None:
        logger.info("assistant: dropping unmapped open-new-tab url=%r", url)
        return None
    return {
        "action": "open-new-tab",
        "url": url,
        "app": page.app,
        "label": page.display_name,
    }


def _validate_open_dialog(
    action: Dict[str, Any], site_map: assistant_site_map.SiteMap,
) -> Optional[Dict[str, Any]]:
    did = action.get("id")
    if not isinstance(did, str) or not did:
        return None
    if not site_map.pages_with_dialog(did):
        logger.info("assistant: dropping unmapped open-dialog id=%r", did)
        return None
    return {"action": "open-dialog", "id": did}


def _validate_open_tab(action: Dict[str, Any], *_args) -> Optional[Dict[str, Any]]:
    label = action.get("label")
    if not isinstance(label, str) or not label.strip():
        return None
    return {"action": "open-tab", "label": label}


def _validate_scroll_to(action: Dict[str, Any], *_args) -> Optional[Dict[str, Any]]:
    selector = action.get("selector")
    if not isinstance(selector, str) or not selector.strip():
        return None
    return {"action": "scroll-to", "selector": selector}


def _validate_highlight(action: Dict[str, Any], *_args) -> Optional[Dict[str, Any]]:
    selector = action.get("selector")
    if not isinstance(selector, str) or not selector.strip():
        return None
    out: Dict[str, Any] = {"action": "highlight", "selector": selector}
    dur = action.get("durationMs")
    if isinstance(dur, (int, float)) and dur > 0:
        out["durationMs"] = int(dur)
    return out


def _validate_support_contacted(action: Dict[str, Any], *_args) -> Optional[Dict[str, Any]]:
    """Post-tool-call receipt the orchestrator emits AFTER
    `contact_support` ran successfully. Confirms to the panel that an
    escalation email went out so the assistant can render a non-
    interactive status row instead of leaving the user wondering.

    Required fields: `recipient` (string, the email the transcript
    went to). Optional: `severity` ("normal"|"emergency"),
    `tenant_admin_count` (int), `subject` (string).
    """
    recipient = action.get("recipient")
    if not isinstance(recipient, str) or not recipient.strip():
        return None
    out: Dict[str, Any] = {"action": "support-contacted", "recipient": recipient}
    severity = action.get("severity")
    if isinstance(severity, str) and severity in ("normal", "emergency"):
        out["severity"] = severity
    tac = action.get("tenant_admin_count")
    if isinstance(tac, (int, float)) and tac >= 0:
        out["tenant_admin_count"] = int(tac)
    subject = action.get("subject")
    if isinstance(subject, str) and subject.strip():
        out["subject"] = subject.strip()
    return out


# Per-name dispatcher. Bound at module load — keeps the validate
# function a tight switch.
_LEAF_VALIDATORS = {
    "navigate": _validate_navigate,
    "open-new-tab": _validate_open_new_tab,
    "open-dialog": _validate_open_dialog,
    "open-tab": _validate_open_tab,
    "scroll-to": _validate_scroll_to,
    "highlight": _validate_highlight,
    "support-contacted": _validate_support_contacted,
}


def validate_action(
    action: Any,
    site_map: assistant_site_map.SiteMap,
    allowlist: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Drop any action that fails vocabulary / allowlist / site-map
    grounding. Returns the normalized action on success, None on
    rejection.

    `chain` recurses; every child gets the same three-layer treatment,
    and a chain whose children all get dropped is itself dropped.
    """
    if not isinstance(action, dict):
        return None
    name = action.get("action")
    if name not in ACTION_VOCABULARY:
        logger.info("assistant: dropping unknown action %r", name)
        return None
    if not _is_action_allowed(name, allowlist):
        logger.info("assistant: dropping tenant-disallowed action %r", name)
        return None
    if name == "chain":
        sub = action.get("actions") or []
        if not isinstance(sub, list):
            return None
        validated_children = []
        for child in sub:
            v = validate_action(child, site_map, allowlist)
            if v is not None:
                validated_children.append(v)
        if not validated_children:
            return None
        return {"action": "chain", "actions": validated_children}

    leaf = _LEAF_VALIDATORS.get(name)
    if leaf is None:
        # Vocabulary check should already have caught this; defensive
        # log so a coding regression is loud.
        logger.warning("assistant: vocabulary listed %r but no validator", name)
        return None
    return leaf(action, site_map)
