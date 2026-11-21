// Dashboard/js/modal.js
// Utility for showing user-friendly, copyable modals
//
// GSEC FX.20 R-037 (SEC-19-002): shared dialog a11y helper (`modA11y`).
// Every Dashboard modal — fragment modals loaded by ui.js's loadModal(),
// the legacy showCopyableModal shell, and the hand-rolled inline overlay
// divs on the page fragments — gets role="dialog"/"alertdialog",
// aria-modal="true", aria-labelledby, a focus trap, Escape-to-close, and
// focus return through this helper.

const FOCUSABLE_SELECTOR = [
    'a[href]',
    'button:not([disabled])',
    'input:not([disabled]):not([type="hidden"])',
    'select:not([disabled])',
    'textarea:not([disabled])',
    '[tabindex]:not([tabindex="-1"])'
].join(',');

let _a11ySeq = 0;

// Returns the element that carries the dialog semantics. For fragment
// modals and the copyable shell the fixed overlay is the root; for the
// inline page modals (e.g. #worker-detail-modal) the overlay root is the
// fixed element and the first child is the card — attach on either.
export function getDialogContainer(el) {
    if (!el || el.nodeType !== 1) return null;
    if (el.classList.contains('fixed') && el.classList.contains('inset-0')) return el;
    let p = el.parentElement;
    while (p) {
        if (p.classList && p.classList.contains('fixed') && p.classList.contains('inset-0')) return p;
        p = p.parentElement;
    }
    return el;
}

// The visible title: the element referenced by aria-labelledby if present,
// else the first heading inside the card.
function getDialogTitle(container) {
    if (container.getAttribute('aria-labelledby')) {
        try {
            const byId = document.getElementById(container.getAttribute('aria-labelledby'));
            if (byId && container.contains(byId)) return byId;
        } catch (_) { /* fall through */ }
    }
    return container.querySelector('h1, h2, h3, h4, [class*="font-semibold"], [class*="font-bold"]');
}

function getFocusables(container) {
    return Array.from(container.querySelectorAll(FOCUSABLE_SELECTOR)).filter((f) => f.offsetWidth > 0 || f.offsetHeight > 0);
}

function getInitialFocus(container) {
    const explicit = container.querySelector('[autofocus]');
    if (explicit) return explicit;
    const focusables = getFocusables(container);
    if (focusables.length) return focusables[0];
    return container;
}

function isDialogVisible(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el.classList.contains('hidden')) return false;
    if (el.getAttribute('hidden') !== null) return false;
    if (el.style.display === 'none') return false;
    return true;
}

function trapKeydown(container, e) {
    if (e.key !== 'Tab') return;
    const focusables = getFocusables(container);
    const first = focusables.length ? focusables[0] : container;
    const last = focusables.length ? focusables[focusables.length - 1] : container;
    const active = document.activeElement;
    const inDialog = active === container || container.contains(active);
    if (!inDialog) {
        // Focus may sit in a stacked dialog on top of this one; let that
        // dialog's own handler keep it there.
        const top = active && active.closest
            ? active.closest('[role="dialog"],[role="alertdialog"]')
            : null;
        if (top && top !== container && isDialogVisible(top)) return;
        e.preventDefault();
        first.focus({ preventScroll: true });
        return;
    }
    if (e.shiftKey) {
        if (active === first || focusables.indexOf(active) === -1) {
            e.preventDefault();
            last.focus({ preventScroll: true });
        }
    } else if (active === last) {
        e.preventDefault();
        first.focus({ preventScroll: true });
    }
}

function restoreFocus(previous) {
    if (!previous || previous.nodeType !== 1) return;
    try {
        if (previous.isConnected) previous.focus({ preventScroll: true });
    } catch (_) { /* element may be gone */ }
}

// Hide a dialog the way the page hides it: clear any inline display the
// opener set (billing-style `style.display='block'` modals) and/or restore
// the `hidden` class. Never leaves a stuck `style.display='none'` behind,
// so class-only reopen paths (e.g. workers.html) keep working.
function _hideDialog(container) {
    if (container.style.display) container.style.display = '';
    container.classList.add('hidden');
}

/**
 * Attach full dialog semantics to an overlay element (or any element
 * inside one): role, aria-modal, aria-labelledby, focus move, focus trap,
 * optional Escape-to-close, and focus return on detach.
 *
 * @param {Element} el        overlay root (fixed inset-0) or the card inside it
 * @param {object}  [opts]
 * @param {'dialog'|'alertdialog'} [opts.role='dialog']
 * @param {boolean} [opts.dismissible=true]  Escape hides the overlay
 *   (clears inline display, adds `hidden`). Set false when the caller
 *   owns its own Escape path (e.g. ui.js alert/confirm helpers).
 * @returns {object|null} { container, detach } or null if el is not a dialog
 */
export function modA11yAttach(el, opts = {}) {
    const container = getDialogContainer(el);
    if (!container) return null;

    // Idempotent: re-attach clears the previous trap/focus state.
    const prev = container._modA11y;
    if (prev) {
        document.removeEventListener('keydown', prev.keyHandler);
        restoreFocus(prev.previousFocus);
        container._modA11y = null;
    }

    const role = opts.role === 'alertdialog' ? 'alertdialog' : 'dialog';
    container.setAttribute('role', role);
    container.setAttribute('aria-modal', 'true');

    const title = getDialogTitle(container);
    if (title) {
        let labelId = title.id;
        if (!labelId) {
            labelId = `mod-a11y-title-${++_a11ySeq}`;
            title.id = labelId;
        }
        container.setAttribute('aria-labelledby', labelId);
    }

    if (container.getAttribute('tabindex') === null) {
        container.setAttribute('tabindex', '-1');
    }

    const keyHandler = (e) => {
        // Self-heal: if the dialog was torn down by a close path that
        // didn't call detach (e.g. a bare el.remove()), drop our handler
        // so we don't leak one keydown listener per removed modal.
        if (!container.isConnected && container._modA11y) {
            container._modA11y.detach();
            return;
        }
        const active = document.activeElement;
        const topDialog = active && active.closest
            ? active.closest('[role="dialog"],[role="alertdialog"]')
            : null;
        // Another (stacked) dialog owns focus — the inner one handles the key.
        if (topDialog && topDialog !== container && isDialogVisible(topDialog)) return;
        if (e.key === 'Escape' && opts.dismissible !== false && isDialogVisible(container)) {
            e.stopPropagation();
            container._modA11y?.detach();
            _hideDialog(container);
            return;
        }
        trapKeydown(container, e);
    };
    document.addEventListener('keydown', keyHandler, true);

    container._modA11y = {
        container,
        keyHandler,
        previousFocus: document.activeElement,
        detach: () => {
            if (!container._modA11y) return;
            document.removeEventListener('keydown', container._modA11y.keyHandler, true);
            restoreFocus(container._modA11y.previousFocus);
            container._modA11y = null;
        }
    };

    // Move focus after the element is in the layout (display toggles are
    // synchronous, but keep focus off-screen-free).
    const initial = getInitialFocus(container);
    if (typeof requestAnimationFrame !== 'undefined') {
        requestAnimationFrame(() => {
            if (container._modA11y) {
                try { initial.focus({ preventScroll: true }); } catch (_) { /* not focusable */ }
            }
        });
    } else {
        try { initial.focus({ preventScroll: true }); } catch (_) { /* not focusable */ }
    }

    return container._modA11y;
}

/** Remove dialog semantics and return focus to the pre-open element. */
export function modA11yDetach(el) {
    const container = getDialogContainer(el);
    if (container && container._modA11y) container._modA11y.detach();
}

export const modA11y = {
    attach: modA11yAttach,
    detach: modA11yDetach,
    getDialogContainer
};

// --- Central auto-attach for hand-rolled inline modal overlays ---------
//
// The Dashboard's page fragments (pages/*.html) contain hand-rolled modal
// overlays: `<div id="...-modal" class="hidden fixed inset-0 ... z-50">`.
// Their open/close paths are scattered across inline <script> blocks and
// per-page modules, so instead of wiring each one by hand we watch the DOM:
// when such an overlay becomes visible (innerHTML swap, hidden-class
// toggle, or display style) it gets dialog semantics, a focus trap,
// Escape-to-close, and focus return — visually unchanged (no layout or
// paint changes, only role/aria/tabindex attributes).
//
// Heuristic for "is a modal overlay": fixed + inset-0 + z-50 + carries an
// id + is not the copyable shell (wired by showCopyableModal) + not a
// known non-modal (the mobile sidebar scrim is z-30, so it's excluded by
// the z-50 requirement).

function isAutoModalCandidate(el) {
    if (!el || el.nodeType !== 1 || !el.id) return false;
    if (el.classList.contains('fixed') !== true || el.classList.contains('inset-0') !== true) return false;
    if (el.classList.contains('z-50') !== true) return false;
    if (el.id === 'copyable-modal' || el.id === 'modal-container' || el.id === 'loading-overlay') return false;
    // A dialog is announced by a title and/or has interactive controls.
    // Transient spinners / empty scrims have neither — skip them so we
    // never move focus into a bare progress overlay.
    if (!el.querySelector('h1, h2, h3, h4, ' + FOCUSABLE_SELECTOR)) return false;
    return true;
}

function _autoWireIfNeeded(container) {
    if (!isAutoModalCandidate(container)) return;
    // Already attached (fragment system attached first, or a previous
    // observer pass during this open) — the _modA11y state carries the
    // trap; leave it alone.
    if (container._modA11y) return;
    // Only wire when it looks like it's being shown (display not none).
    if (!isDialogVisible(container)) return;
    if (typeof getComputedStyle !== 'undefined') {
        let disp = 'block';
        try { disp = getComputedStyle(container).display; } catch (_) { /* no layout */ }
        if (disp === 'none') return;
    }
    // Auto-wired inline modals keep their OWN close behavior. Many of them
    // (queue, add-worker, collision-resolve, bulk-skip, …) already register
    // their own document-level Escape handler and close via el.remove();
    // claiming Escape here would double-close and break that cleanup, and
    // some (e.g. worker-detail) are not Escape-dismissible at all. So the
    // auto-attach adds only role/aria + focus trap + focus return. Set
    // data-mod-a11y-escape="1" on an overlay to also let this helper close
    // it on Escape.
    modA11yAttach(container, {
        role: container.dataset.modA11yRole === 'alertdialog' ? 'alertdialog' : 'dialog',
        dismissible: container.dataset.modA11yEscape === '1'
    });
}

function _scanAutoModals(root) {
    if (!root || !root.querySelectorAll) return;
    const els = root.querySelectorAll ? root.querySelectorAll('.fixed.inset-0.z-50[id]') : [];
    for (const el of els) _autoWireIfNeeded(el);
}

if (typeof MutationObserver !== 'undefined') {
    const _detachIfPresent = (node) => {
        if (!node || node.nodeType !== 1) return;
        if (node._modA11y) { node._modA11y.detach(); return; }
        if (node.querySelectorAll) {
            for (const d of node.querySelectorAll('[role="dialog"],[role="alertdialog"]')) {
                if (d._modA11y) d._modA11y.detach();
            }
        }
    };
    const _bodyObserver = new MutationObserver((mutations) => {
        for (const m of mutations) {
            for (const node of m.addedNodes) {
                if (node.nodeType !== 1) continue;
                if (isAutoModalCandidate(node)) _autoWireIfNeeded(node);
                _scanAutoModals(node);
            }
            // Close paths that .remove() the overlay (queue, add-worker,
            // collision-resolve, bulk-skip) still get their focus returned.
            for (const node of m.removedNodes) _detachIfPresent(node);
            if (m.type === 'attributes' && m.target && isAutoModalCandidate(m.target)) {
                // Close paths that only toggle class/display (without
                // calling modA11y.detach) still get their focus returned.
                if (m.target._modA11y && !isDialogVisible(m.target)) {
                    m.target._modA11y.detach();
                    continue;
                }
                _autoWireIfNeeded(m.target);
            }
        }
    });
    const _startObserving = () => {
        if (document.body) {
            _bodyObserver.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style'] });
        } else if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', _startObserving, { once: true });
        }
    };
    _startObserving();
}

// Exposed for plain <script> pages / inline fragment scripts / tests.
if (typeof window !== 'undefined') {
    window.modA11y = modA11y;
}

/**
 * @deprecated Use `showAlertWithAction({ title, message, variant, actionLabel, onAction })`
 *   from `./ui.js` for new alert flows. That helper matches the rest of the
 *   Dashboard alert design system (top-positioned w-96 card, semantic icon,
 *   indigo primary button) and supports an optional one-click navigation
 *   action button. `showCopyableModal` remains exported only for legacy
 *   pages (promo-codes.html, geographic-pricing.html) that still depend on
 *   the wide copyable shell; remove once those are migrated.
 */
export function showCopyableModal(title, message) {
    // Remove existing modal if any
    const existingModal = document.getElementById('copyable-modal');
    if (existingModal) {
        modA11yDetach(existingModal);
        existingModal.remove();
    }

    // Create modal HTML
    const modalHtml = `
        <div id="copyable-modal" class="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50 flex items-center justify-center">
            <div class="relative mx-auto p-5 border w-11/12 max-w-2xl shadow-lg rounded-md bg-white">
                <!-- Header -->
                <div class="flex justify-between items-center pb-3 border-b">
                    <h3 class="text-xl font-semibold text-gray-900">${escapeHtml(title)}</h3>
                    <button onclick="window.modA11y && window.modA11y.detach(document.getElementById('copyable-modal')); document.getElementById('copyable-modal').remove()"
                            class="text-gray-400 hover:text-gray-600 text-2xl leading-none" aria-label="Close">
                        &times;
                    </button>
                </div>

                <!-- Content -->
                <div class="mt-4">
                    <pre class="whitespace-pre-wrap text-sm text-gray-700 bg-gray-50 p-4 rounded border border-gray-200 select-all cursor-text">${escapeHtml(message)}</pre>
                </div>

                <!-- Footer -->
                <div class="flex justify-end mt-4 space-x-2">
                    <button onclick="window.modCopyText(document.querySelector('#copyable-modal pre').textContent, this)"
                            class="px-4 py-2 bg-primary/80 text-white text-sm font-medium rounded hover:bg-primary/90 focus:outline-none focus:ring-2 focus:ring-primary">
                        Copy to Clipboard
                    </button>
                    <button onclick="window.modA11y && window.modA11y.detach(document.getElementById('copyable-modal')); document.getElementById('copyable-modal').remove()"
                            class="px-4 py-2 bg-gray-200 text-gray-700 text-sm font-medium rounded hover:bg-gray-300 focus:outline-none focus:ring-2 focus:ring-gray-400">
                        Close
                    </button>
                </div>
            </div>
        </div>
    `;

    // Add to DOM
    document.body.insertAdjacentHTML('beforeend', modalHtml);

    // Dialog semantics + focus trap + focus return (FX.20 R-037).
    // Escape and backdrop-close go through the existing paths below; the
    // a11y helper adds role/aria, the trap and focus return.
    modA11yAttach(document.getElementById('copyable-modal'), { dismissible: false });

    // Close on ESC key
    const escapeHandler = (e) => {
        if (e.key === 'Escape') {
            const m = document.getElementById('copyable-modal');
            if (m) {
                modA11yDetach(m);
                m.remove();
            }
            document.removeEventListener('keydown', escapeHandler);
        }
    };
    document.addEventListener('keydown', escapeHandler);

    // Close on backdrop click
    document.getElementById('copyable-modal').addEventListener('click', (e) => {
        if (e.target.id === 'copyable-modal') {
            modA11yDetach(e.target);
            e.target.remove();
        }
    });
}

function escapeHtml(text) {
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return text.replace(/[&<>"']/g, m => map[m]);
}
