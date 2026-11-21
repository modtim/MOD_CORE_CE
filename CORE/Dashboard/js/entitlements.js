// GT.D11a — shared tier-aware entitlement helpers for the Dashboard.
//
// The server stays authoritative: every gated action still 402s when the
// capability is absent. These helpers are a CLIENT-SIDE convenience that
// only gate (disable + badge) when a capability is ABSENT from the
// /platform/features `entitlements` block. If the block is missing or
// unreadable we fail OPEN (treat everything as unlocked) so a missing
// feature never locks out a paid deployment.
//
// The block is sourced from the same coalesced 30s /platform/features cache
// ui.js already uses, so this adds no extra request on page load.

import { getPlatformFeatures } from './api.js';

// Upgrade / billing page hash route (the tenant-facing Upgrade link target).
const UPGRADE_HASH = 'billing';

let _cache = { loaded: false, entitlements: null };

async function _load() {
    if (_cache.loaded) return _cache;
    let feats = null;
    try {
        feats = await getPlatformFeatures();
    } catch (_) {
        feats = null; // offline / 401 — fail open below
    }
    const block = (feats && typeof feats === 'object' && feats.entitlements) ? feats.entitlements : null;
    _cache = { loaded: true, entitlements: block };
    return _cache;
}

// Resolve the entitlements block (awaitable) — also used by the badge so the
// lock state re-evaluates once the feature payload arrives.
function _entitlements() {
    return _load();
}

window.MODEntitlements = {
    // Synchronous, best-effort read of the already-loaded block. Pages that
    // call this in an async init (after an await) get the real value; a
    // call before the first load resolves to "unresolved" (fail closed).
    get _block() { return _cache.entitlements; },

    async has(cap) {
        const b = (await _entitlements()).entitlements;
        if (!b || !Array.isArray(b.capabilities)) return false; // fail closed
        return b.capabilities.includes(cap);
    },

    async limit(name) {
        const b = (await _entitlements()).entitlements;
        if (!b || !b.limits || !(name in b.limits)) return null; // unknown
        return b.limits[name]; // int | "unlimited"
    },

    async edition() {
        const b = (await _entitlements()).entitlements;
        return (b && b.edition) ? b.edition : 'unknown';
    },

    async tier() {
        const b = (await _entitlements()).entitlements;
        return (b && b.tier_name) ? b.tier_name : 'unknown';
    },

    // GT.D11.s1/s2 — the GY.C0 declared install environment + what it needs
    // (entitlements.compliance off /platform/features). Resolves to null when
    // the payload carries no compliance sub-block, so callers render nothing
    // rather than an empty claim. On the CE carve the map module is stripped
    // server-side, so `needs` is [] and only the declared values arrive — we
    // NEVER invent a needs list client-side.
    async needs() {
        const b = (await _entitlements()).entitlements;
        const c = b && b.compliance;
        if (!c || typeof c !== 'object') return null;
        return {
            declared: Array.isArray(c.declared_environment)
                ? c.declared_environment.filter((e) => typeof e === 'string') : [],
            needs: Array.isArray(c.needs)
                ? c.needs.filter((n) => n && typeof n === 'object' && n.id) : [],
            edition: c.edition || 'unknown',
            map_version: c.map_version || 0,
        };
    },

    // Gate a control when `cap` is ABSENT. If present (or unknown) the element
    // is left untouched. Idempotent: re-calling never stacks badges.
    async lockElement(el, cap, label) {
        if (!el) return;
        if (await this.has(cap)) return; // granted (or unresolved) → leave as-is
        if (el.dataset.modLocked === '1') return; // already locked

        el.dataset.modLocked = '1';
        el.classList.add('mod-locked');
        el.disabled = true;
        if (!el.title) {
            el.title = label ? `${label} — not part of this edition`
                             : 'Not part of this edition';
        }

        const badge = document.createElement('span');
        badge.className = 'mod-paid-badge';
        const a = document.createElement('a');
        a.href = `#${UPGRADE_HASH}`;
        a.textContent = 'Not part of this edition';
        a.addEventListener('click', (e) => {
            // The element is disabled, so let the badge link itself navigate.
            e.stopPropagation();
        });
        badge.appendChild(a);
        // Attach next to the control rather than inside it: wrapping a
        // <button> would break its click target.
        el.insertAdjacentElement('afterend', badge);
    },
};

export default window.MODEntitlements;
