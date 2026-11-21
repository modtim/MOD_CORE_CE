'use strict';
// Dashboard/js/shared/assistant-actions.js
//
// MOD Assistant — Dashboard-side shared ACTION runtime (mod-assistant-agent §6.4).
//
// This is the Dashboard mirror of CORE/WorkflowBuilder/lib/shared/assistant-actions.ts:
// the app-facing entry point for the client-side action runtime the panels call
// (navigate / open-new-tab / open-dialog, plus open-tab, scroll-to, highlight, chain).
//
// ONE SOURCE OF TRUTH. The canonical implementation lives in
// CORE/WorkflowBuilder/lib/assistant-nav/navActions.ts, which the Dashboard build
// compiles into js/vendor/assistant-nav.js (regenerate:
// `node CORE/WorkflowBuilder/lib/assistant-nav/build-dashboard.js`) and which
// index.html:68 loads, as a plain deferred script, BEFORE the pages that use it
// (js/assistant-launcher.js :71, js/assistant-panel.js :74). This module RE-EXPORTS
// that runtime; it NEVER reimplements, copies, or weakens a single check. A second
// copy of the validation is exactly the drift hazard §6.4 was created to eliminate.
//
// Why this module exists at all: js/settings.js must not carry its own action
// vocabulary (the admin allowlist UX used to hardcode one, which could drift from
// the runtime's KNOWN_ACTION_TYPES and offer a checkbox the client then drops
// silently). Settings asks THIS module for the list instead, so the checkbox set
// and the executable set are the same set by construction.
//
// Everything resolves LAZILY, at call time — never at module load — so a missing
// vendor artifact degrades to the caller's fallback (console.warn once) instead of
// throwing at import time and hard-breaking the Settings page.
//
// Security note: the same-origin / shape allow-list (isValidRoute, isSafeId,
// isSafeAnchorSelector, isKnownActionType) and the server-side site map + per-tenant
// allow-list in services/assistant_actions.py are the security logic. They are not
// reimplemented or weakened here. No URL is ever built here, no DOM is touched here,
// no token is read here, and no host/domain/IP appears here — cross-app URLs come
// only from window.APP_CONFIG at runtime, in the panel wrapper that already does it.

// js/vendor/assistant-nav.js:70-71 — "support-contacted is a receipt payload,
// not something `executeAction` runs". The admin allowlist UX must not offer an
// enable/disable checkbox for an action the user cannot enable/disable, so it is
// filtered out of the TOGGLEABLE list even though the runtime knows the name.
const NON_TOGGLEABLE = ['support-contacted'];

// Probe order only — the runtime is the authority. js/vendor/assistant-nav.js:72-81
// exposes KNOWN_ACTION_TYPES as a non-iterable Set inside the generated closure, so
// the module asks the runtime, per candidate name, whether it knows that action type.
const CANDIDATE_ACTION_NAMES = [
    'navigate',
    'open-new-tab',
    'open-dialog',
    'open-tab',
    'scroll-to',
    'highlight',
    'chain',
    'support-contacted',
];

// Lazy on every call: the vendor artifact sets window.ModAssistantNav.
function _canonical() {
    return window.ModAssistantNav;
}

// The action names the runtime accepts, minus the non-toggleable receipt payload.
// [] when the runtime is absent — the caller then fail-opens to its own fallback
// list (render only) and logs once.
export function actionVocabulary() {
    const canonical = _canonical();
    if (!canonical) return [];
    const known = [];
    for (const name of CANDIDATE_ACTION_NAMES) {
        if (canonical.isKnownActionType(name)) known.push(name);
    }
    return known.filter((name) => NON_TOGGLEABLE.indexOf(name) === -1);
}

export function isKnownActionType(name) {
    const canonical = _canonical();
    return canonical ? canonical.isKnownActionType(name) : false;
}

// Fail-CLOSED: no runtime = no action. Never guess a schema here.
export function validateAction(raw) {
    const canonical = _canonical();
    return canonical ? canonical.validateAction(raw) : null;
}

// Fail-CLOSED: no runtime = no action. `opts` carries the app-specific
// resolveNavigateUrl / onOpenDialog callbacks (Dashboard reads window.APP_CONFIG
// in js/assistant-panel.js; that stays in the wrapper, not here).
export function executeAction(raw, opts) {
    const canonical = _canonical();
    return canonical ? canonical.executeAction(raw, opts) : null;
}

export function available() {
    return typeof _canonical() === 'object';
}

// Published for the non-module consumers (js/settings.js is an ES module, but the
// call site reaches it through the same global the vendor artifact uses).
window.ModAssistantActions = {
    actionVocabulary,
    isKnownActionType,
    validateAction,
    executeAction,
    available,
};
