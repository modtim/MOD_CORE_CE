// Dashboard/js/help-mindmap.js
//
// Help → Mind Map tab. Plain HTML/CSS tree (no canvas, no
// layout engine). Each node is a column containing a box plus,
// when expanded, a row of child columns. Click a box to:
//   - update the always-visible detail aside on the right
//   - if it has children, toggle that branch's expansion and
//     collapse any other branch that isn't on its ancestor path
//
// Predictable: no zoom, no pan, no async layout waterfall. The
// browser handles all the layout via flex. Vertical page scroll
// inside the tree container is the only navigation when the tree
// grows tall.
//
// Data: `${DOCS_URL}/help-tree.json` (proxied to the dashboard
// origin via nginx, see CORE/Dashboard/nginx.conf).

// Box and aside labels use the sidebar theme name when node.id equals a
// product-names map key (root 'help', 'workflow-builder', ...); every other
// node keeps its docs label. Flag OFF or unknown key -> node.label unchanged.
import { themeLabel } from './product-names.js';

function nodeLabel(node) {
    return themeLabel(node.id, node.label);
}

const MIND_MAP_BREAKPOINT_PX = 768;

// Tree cache — survives across navigateToPage calls (module-scoped)
// AND across full-page reloads (sessionStorage). Tree shape is
// generated at docs-build time and only changes when docs change,
// so reusing a stale tree for an entire session is fine; a fresh
// tab gets the new shape on first fetch.
const TREE_CACHE_KEY = 'help.mindmap.tree.v1';

let _treeCache = null;
let _parentMap = null;
let _expandedIds = new Set();
// The canvas DOM that we most recently rendered into. navigateToPage
// wipes the page-content innerHTML which gives us a NEW canvas
// element on every visit to /help. We compare identity to detect
// "this is a fresh canvas, re-render into it" without re-fetching
// the tree.
let _rootEl = null;

export async function mountHelpMindMap(rootEl) {
    if (!rootEl) return;

    // Re-mount against a fresh canvas (same session, same tree) is
    // cheap: in-memory tree + DOM render only, no network.
    const isFreshCanvas = rootEl !== _rootEl;

    // Tree resolution: memory → sessionStorage → network.
    if (!_treeCache) {
        const cached = readSessionCache();
        if (cached) {
            _treeCache = cached;
            _parentMap = buildParentMap(cached);
        }
    }
    if (!_treeCache) {
        const tree = await fetchTree();
        _treeCache = tree;
        _parentMap = buildParentMap(tree);
        writeSessionCache(tree);
    }

    // If the canvas hasn't changed AND already holds our rendered
    // tree, this is a no-op. Saves the DOM rebuild on tab toggles
    // back to Mind Map within the same Help page visit.
    if (!isFreshCanvas && rootEl.querySelector('.hmm-scroll')) {
        return;
    }

    _rootEl = rootEl;

    // Strip the skeleton spinner once we have data.
    const skeleton = rootEl.querySelector('#help-mindmap-skeleton');
    if (skeleton) skeleton.remove();

    render();
}

// --- Tree fetch -------------------------------------------------------

async function fetchTree() {
    const url = '/help-tree.json';
    const resp = await fetch(url, { credentials: 'omit' });
    if (!resp.ok) {
        throw new Error(`help-tree fetch failed: HTTP ${resp.status}`);
    }
    const data = await resp.json();
    if (!data || !data.root) {
        throw new Error('help-tree response missing `root`');
    }
    return data;
}

function readSessionCache() {
    try {
        const raw = sessionStorage.getItem(TREE_CACHE_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        if (parsed && parsed.root) return parsed;
    } catch {
        /* malformed cache — fall through to network */
    }
    return null;
}

function writeSessionCache(tree) {
    try {
        sessionStorage.setItem(TREE_CACHE_KEY, JSON.stringify(tree));
    } catch {
        /* sessionStorage full or disabled — non-fatal, runtime cache is enough */
    }
}

function buildParentMap(tree) {
    const map = new Map();
    function visit(node, parentId) {
        if (parentId !== null) map.set(node.id, parentId);
        if (Array.isArray(node.children)) {
            for (const c of node.children) visit(c, node.id);
        }
    }
    visit(tree.root, null);
    return map;
}

function ancestorIds(id) {
    const set = new Set([id]);
    let cur = _parentMap.get(id);
    while (cur) {
        set.add(cur);
        cur = _parentMap.get(cur);
    }
    return set;
}

// --- Render -----------------------------------------------------------

function render() {
    if (!_rootEl || !_treeCache) return;
    _rootEl.innerHTML = '';
    _rootEl.classList.add('help-mindmap-tree');
    const scroll = document.createElement('div');
    scroll.className = 'hmm-scroll';
    scroll.appendChild(renderNode(_treeCache.root, true));
    _rootEl.appendChild(scroll);
}

function renderNode(node, isRoot) {
    const hasChildren = Array.isArray(node.children) && node.children.length > 0;
    const expanded = _expandedIds.has(node.id);

    const wrap = document.createElement('div');
    wrap.className = 'hmm-node';
    wrap.dataset.nodeId = node.id;

    const box = document.createElement('button');
    box.type = 'button';
    box.className = 'hmm-box';
    if (isRoot) box.classList.add('hmm-box-root');
    else if (hasChildren) box.classList.add('hmm-box-category');
    else box.classList.add('hmm-box-leaf');
    if (expanded) box.classList.add('hmm-expanded');

    // In left-right mode the chevron is horizontal: ▶ collapsed,
    // ▼ expanded reads as "this opens downward" but here the
    // children appear to the RIGHT of the box, so ▶ stays for
    // collapsed and we flip to a stronger filled glyph when
    // expanded so the user sees the state change.
    const chevron = hasChildren && !isRoot
        ? ` ${expanded ? '◀' : '▶'}`
        : '';
    box.textContent = nodeLabel(node) + chevron;
    box.addEventListener('click', () => onBoxClick(node));
    wrap.appendChild(box);

    if (hasChildren && (isRoot || expanded)) {
        // Split children into two groups: non-terminating
        // (categories with their own children) get bigger
        // vertical gaps between them so each branch can breathe;
        // terminating (leaves) stack tightly together at the
        // bottom of the column.
        const categories = [];
        const leaves = [];
        for (const c of node.children) {
            const cHas = Array.isArray(c.children) && c.children.length > 0;
            (cHas ? categories : leaves).push(c);
        }
        const colWrap = document.createElement('div');
        colWrap.className = 'hmm-children';
        if (categories.length > 0) {
            const catCol = document.createElement('div');
            catCol.className = 'hmm-children-categories';
            for (const c of categories) catCol.appendChild(renderNode(c, false));
            colWrap.appendChild(catCol);
        }
        if (leaves.length > 0) {
            const leafCol = document.createElement('div');
            leafCol.className = 'hmm-children-leaves';
            for (const c of leaves) leafCol.appendChild(renderNode(c, false));
            colWrap.appendChild(leafCol);
        }
        wrap.appendChild(colWrap);
    }
    return wrap;
}

// --- Click handler ----------------------------------------------------

function onBoxClick(node) {
    // Detail aside always updates first. It's a sibling to the
    // tree and doesn't care about expansion state.
    openLeafDetail(node);

    const hasChildren = Array.isArray(node.children) && node.children.length > 0;
    if (!hasChildren) return; // pure leaf — detail only

    // Root: collapse the world back to the initial first-ring view.
    if (node.id === _treeCache.root.id) {
        _expandedIds.clear();
        render();
        return;
    }

    const isExpanded = _expandedIds.has(node.id);
    if (isExpanded) {
        // Collapse THIS node (and the implicit collapse of all its
        // descendants happens automatically since render() only
        // recurses into expanded children).
        _expandedIds.delete(node.id);
    } else {
        // Expanding: focus this branch. Collapse every currently-
        // expanded node that isn't on this node's ancestor path so
        // the new children have nothing visually competing with them.
        const keep = ancestorIds(node.id);
        for (const id of Array.from(_expandedIds)) {
            if (!keep.has(id)) _expandedIds.delete(id);
        }
        _expandedIds.add(node.id);
    }
    render();

    // After the new DOM is laid out, scroll the just-clicked box
    // back into view. This is the analog of "pan to center" but
    // uses the native browser smooth-scroll API — no layout math,
    // no animation pipeline to fight.
    requestAnimationFrame(() => {
        const fresh = _rootEl.querySelector(`[data-node-id="${cssEsc(node.id)}"] > .hmm-box`);
        if (fresh && fresh.scrollIntoView) {
            fresh.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' });
        }
    });
}

function cssEsc(s) {
    return String(s).replace(/(["\\])/g, '\\$1');
}

// --- Detail aside (always-visible, populated on every click) ----------

function openLeafDetail(node) {
    const titleEl = document.getElementById('help-leaf-title');
    const descEl = document.getElementById('help-leaf-description');
    if (titleEl) titleEl.textContent = nodeLabel(node);
    if (descEl) {
        descEl.textContent = node.description
            || (Array.isArray(node.children) && node.children.length
                ? "Expand to see what's inside."
                : '');
    }

    const docsBase = (window.APP_CONFIG && window.APP_CONFIG.DOCS_URL) || '';
    const docsBtn = document.getElementById('help-leaf-docs');
    if (docsBtn) {
        if (node.docs_slug) {
            docsBtn.href = docsBase.replace(/\/$/, '') + node.docs_slug;
            docsBtn.style.display = 'inline-flex';
            docsBtn.classList.remove('hidden');
        } else {
            docsBtn.style.display = 'none';
        }
    }

    const featureBtn = document.getElementById('help-leaf-feature');
    const featureLabel = document.getElementById('help-leaf-feature-label');
    if (featureBtn) {
        if (node.dashboard_route) {
            featureBtn.href = node.dashboard_route;
            featureBtn.target = '_self';
            if (featureLabel) featureLabel.textContent = 'Go to feature';
            featureBtn.style.display = 'inline-flex';
            featureBtn.classList.remove('hidden');
        } else if (node.wb_route) {
            const wfBase = (window.APP_CONFIG && window.APP_CONFIG.WORKFLOWS_URL) || '';
            featureBtn.href = wfBase.replace(/\/$/, '') + node.wb_route;
            featureBtn.target = '_self';
            if (featureLabel) featureLabel.textContent = 'Open in Workflow Builder';
            featureBtn.style.display = 'inline-flex';
            featureBtn.classList.remove('hidden');
        } else {
            featureBtn.style.display = 'none';
        }
    }
}
