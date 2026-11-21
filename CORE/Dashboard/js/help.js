// Dashboard/js/help.js
//
// Help page bootstrap. Wires the card-grid deep links to the current
// docs URL and binds the Cards / Mind Map tab switcher.
//
// Tab choice is persisted in localStorage under `help.activeTab` so
// the user lands on whichever view they last used. Cards is the
// default — the Mind Map module is heavier (Cytoscape + help-tree
// fetch) and lazy-loaded only on the first activation per session.

import { me, isPaidEdition } from './api.js';
import { initializeAiInventoryPage } from './ai-inventory.js';
import { initializeSupportTicketsPage } from './support-tickets.js';

const STORAGE_KEY = 'help.activeTab';
const VALID_TABS = new Set(['cards', 'mindmap', 'ai-inventory', 'support-tickets']);

// 0b.2 redo (R37.4 part 2, owner 10-08: "tabs put within the main tab"): AI
// in this platform + Support are tabs of THIS page, not sidebar links. Same
// help-tab / help-panel-<key> convention the Cards/Mind-Map strip already uses
// (localStorage 'help.activeTab', NOT ?tab=), same fetch-verbatim + load-once
// child mechanism the Security/Compliance hubs use, and the SAME role gates
// the old sidebar links used in ui.js updateUIVisibility (ai-inventory =
// platform admin only; support-tickets = every signed-in user).
const _CHILD_PAGE = {
    'ai-inventory': 'ai-inventory',
    'support-tickets': 'support-tickets',
};

const _CHILD_INIT = {
    'ai-inventory': () => initializeAiInventoryPage(),
    'support-tickets': () => initializeSupportTicketsPage(),
};

const _TAB_GATE = {
    'ai-inventory': 'platform',
    // 2400LR-cdu: 'paid' = hidden on CE (see the gate in initializeHelpPage).
    'support-tickets': 'paid',
};

let _isPlatformAdmin = false;
let _childLoaded = {};
// 2400LR-cdu: routers/tenant/support_tickets.py denied on CE (manifest:1327); fail-closed: unknown edition -> hide.
let _isPaidEdition = true;

function _tabAllowed(key) {
    if (!_TAB_GATE[key]) return true;   // cards / mindmap: ungated, as before
    switch (_TAB_GATE[key]) {
        case 'all': return true;
        case 'platform': return _isPlatformAdmin;
        // 2400LR-cdu: routers/tenant/support_tickets.py denied on CE (manifest:1327); fail-open on unknown edition.
        case 'paid': return _isPaidEdition;
        default: return false;
    }
}

export async function initializeHelpPage(initialTab = null) {
    _childLoaded = {};
    try {
        const currentUser = await me.get();
        _isPlatformAdmin = !!currentUser.is_platform_admin;
    } catch (_) { /* stay hidden — fail closed on an unreadable /me */ }
    // 2400LR-cdu: routers/tenant/support_tickets.py denied on CE (manifest:1327); fail-open on unknown edition.
    // Read AFTER /me so the paid-edition request order is unchanged; isPaidEdition
    // is fail-closed and served off the coalesced /platform/features cache.
    _isPaidEdition = await isPaidEdition();

    const docsBase = (window.APP_CONFIG && window.APP_CONFIG.DOCS_URL) || 'https://example.com';
    const apiBase = (window.APP_CONFIG && window.APP_CONFIG.API_URL) || '';

    const links = {
        'help-getting-started': docsBase + '/getting-started',
        'help-workflow-builder': docsBase + '/workflow-builder',
        'help-node-library': docsBase + '/node-library',
        'help-api-reference': apiBase + '/docs',
        'help-worker-setup': docsBase + '/worker-setup',
        'help-troubleshooting': docsBase + '/troubleshooting',
        'help-full-docs': docsBase,
    };

    Object.entries(links).forEach(([id, url]) => {
        const el = document.getElementById(id);
        if (el) el.href = url;
    });

    // Tab wiring.
    const tabs = Array.from(document.querySelectorAll('.help-tab'));
    if (tabs.length === 0) return;
    for (const btn of tabs) {
        const key = btn.dataset.helpTab;
        if (!VALID_TABS.has(key)) continue;
        // Role-gated tabs (the two folded children) ship hidden; reveal the
        // ones this user's role allows, same gates the old sidebar links used.
        if (_tabAllowed(key)) btn.classList.remove('hidden');
        btn.addEventListener('click', () => {
            const next = btn.dataset.helpTab;
            if (!VALID_TABS.has(next) || !_tabAllowed(next)) return;
            activateTab(next);
            try {
                localStorage.setItem(STORAGE_KEY, next);
            } catch {}
        });
    }
    const saved = readSavedTab();
    const initial = initialTab || saved;
    activateTab(_tabAllowed(initial) ? initial : 'cards');
}

function readSavedTab() {
    try {
        const v = localStorage.getItem(STORAGE_KEY);
        if (VALID_TABS.has(v)) return v;
    } catch {}
    return 'cards';
}

function activateTab(name) {
    // Tab buttons: swap the underline + color.
    for (const btn of document.querySelectorAll('.help-tab')) {
        const active = btn.dataset.helpTab === name;
        btn.classList.toggle('border-primary', active);
        btn.classList.toggle('text-primary', active);
        btn.classList.toggle('border-transparent', !active);
        btn.classList.toggle('text-gray-500', !active);
    }
    // Panels: only one visible at a time (generic over every help-panel-*,
    // including the two folded children — same behaviour for cards/mindmap).
    for (const panel of document.querySelectorAll('.help-panel')) {
        const key = panel.id.replace('help-panel-', '');
        if (!VALID_TABS.has(key)) continue;
        panel.classList.toggle('hidden', key !== name);
    }
    // Folded children: markup loads verbatim from pages/<child>.html into the
    // panel once per page load, then the child's own initializer runs (re-
    // clicking the tab must NOT re-init: the child re-binds its own listeners).
    if (_CHILD_PAGE[name] && !_childLoaded[name]) {
        _childLoaded[name] = true;
        const panel = document.getElementById(`help-panel-${name}`);
        fetch(`/pages/${_CHILD_PAGE[name]}.html`)
            .then((res) => res.ok ? res.text() : null)
            .then((html) => {
                if (html && panel) panel.innerHTML = html;
                const init = _CHILD_INIT[name];
                return init ? init() : null;
            })
            .catch(() => { /* child loaders already fail-soft in-page */ });
    }

    // Lazy-import the Mind Map module on first activation. Cards-only
    // users pay zero bundle cost for the module path. The module
    // itself caches the tree JSON in sessionStorage AND the
    // mountHelpMindMap call is idempotent against the canvas — see
    // help-mindmap.js. So calling mountMindMap() on every navigation
    // is correct: it hits the in-memory tree cache (fast) when the
    // canvas was just rebuilt by navigateToPage's innerHTML wipe.
    if (name === 'mindmap') void mountMindMap();
}

// Module-scoped — survives across navigateToPage calls. Caches the
// dynamic import only; the actual mount-state lives on the module's
// own _treeCache (see help-mindmap.js).
let _helpMindMapModule = null;

async function mountMindMap() {
    try {
        if (_helpMindMapModule === null) {
            _helpMindMapModule = await import('./help-mindmap.js');
        }
        const canvas = document.getElementById('help-mindmap-canvas');
        if (canvas) await _helpMindMapModule.mountHelpMindMap(canvas);
    } catch (err) {
        console.error('help: mind map failed to mount', err);
        const sk = document.getElementById('help-mindmap-skeleton');
        if (sk) sk.textContent = 'Could not load the mind map. Reload the page to try again.';
    }
}
