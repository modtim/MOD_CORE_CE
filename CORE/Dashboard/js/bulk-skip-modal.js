// Dashboard/js/bulk-skip-modal.js
//
// data-files-launch-bulletproof §2 — warning toast + skipped-items modal
// for bulk operations on the Files tab. Used when `runBulkAction` returns
// `skipped.length > 0`. Reason → human copy mapping is the single source
// of truth on the dashboard side; adding a new reason requires updating
// REASON_HUMAN_COPY here AND REASON_CODES in bulk-aggregator.js.

const REASON_HUMAN_COPY = Object.freeze({
    not_owner: "You're not the owner of this file",
    no_permission: "You don't have permission to {verb} this file",
    system_generated: "This is a system folder and can't be modified",
    audit_locked: "Locked for audit (workflow run output)",
    not_found: 'Already deleted or moved',
    name_collision: 'A file with this name already exists in the destination',
    quota_exceeded: 'Storage is full — free up space to continue.',
    unknown: 'Could not complete this operation',
});

function reasonToHuman(reason, verb) {
    const tpl = REASON_HUMAN_COPY[reason] || REASON_HUMAN_COPY.unknown;
    return tpl.replace('{verb}', verb || 'use');
}

/**
 * Show a warning toast announcing N skipped items + a "See the list (N)"
 * link that opens the modal. Existing success notification handling is
 * the caller's responsibility for the `succeeded.length` half.
 *
 * @param {Object} options
 * @param {string} options.verb e.g. "delete", "move", "copy"
 * @param {Array<{id, name, reason}>} options.skippedItems
 */
export function showBulkSkipWarning({ verb, skippedItems }) {
    if (!Array.isArray(skippedItems) || skippedItems.length === 0) return;

    const past = pastTense(verb);
    const toast = document.createElement('div');
    toast.id = 'bulk-skip-toast';
    toast.className =
        'fixed top-4 right-4 z-50 max-w-md bg-yellow-50 border-l-4 border-yellow-500 ' +
        'rounded-lg shadow-lg p-4 flex items-start gap-3';
    toast.innerHTML = `
        <svg class="w-5 h-5 text-yellow-600 flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/>
        </svg>
        <div class="flex-1">
            <p class="text-sm font-semibold text-yellow-900">
                Not all files could be ${escapeHtml(past)}
            </p>
            <button id="bulk-skip-see-list" class="mt-1 text-sm text-yellow-700 hover:text-yellow-900 underline">
                See the list (${skippedItems.length})
            </button>
        </div>
        <button id="bulk-skip-toast-close" class="text-yellow-600 hover:text-yellow-800 flex-shrink-0" aria-label="Dismiss">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
            </svg>
        </button>
    `;
    document.body.appendChild(toast);

    const close = () => toast.remove();
    document.getElementById('bulk-skip-toast-close').addEventListener('click', close);
    document.getElementById('bulk-skip-see-list').addEventListener('click', () => {
        renderBulkSkipModal({ verb, skippedItems });
    });

    // Auto-dismiss after 10 seconds — gives the user enough time to see
    // and click "See the list" without being annoying.
    setTimeout(() => {
        if (document.body.contains(toast)) toast.remove();
    }, 10_000);
}

/**
 * Render the modal listing each skipped item + its reason. Closes on
 * Escape, outside-click, or close-button click.
 */
export function renderBulkSkipModal({ verb, skippedItems }) {
    // Tear down any existing modal so re-clicks don't stack.
    const existing = document.getElementById('bulk-skip-modal');
    if (existing) existing.remove();

    const past = pastTense(verb);
    const modal = document.createElement('div');
    modal.id = 'bulk-skip-modal';
    modal.className =
        'fixed inset-0 z-50 bg-black bg-opacity-50 flex items-center justify-center p-4';

    const rowsHtml = skippedItems
        .map(
            (item) => `
        <li class="flex items-start justify-between py-2 px-3 hover:bg-gray-50 border-b border-gray-100 last:border-b-0">
            <div class="flex-1 min-w-0 mr-3">
                <p class="text-sm font-medium text-gray-900 truncate">${escapeHtml(item.name || `Item #${item.id}`)}</p>
                <p class="text-xs text-gray-500 mt-0.5">${escapeHtml(reasonToHuman(item.reason, verb))}</p>
            </div>
        </li>`,
        )
        .join('');

    modal.innerHTML = `
        <div class="bg-white rounded-xl shadow-2xl max-w-lg w-full max-h-[80vh] flex flex-col">
            <div class="flex items-center justify-between p-4 border-b border-gray-200">
                <h3 class="text-lg font-semibold text-gray-800">Could not be ${escapeHtml(past)} (${skippedItems.length})</h3>
                <button id="bulk-skip-modal-close" class="text-gray-400 hover:text-gray-600" aria-label="Close">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
                    </svg>
                </button>
            </div>
            <ul class="overflow-y-auto flex-1 divide-y divide-gray-100">
                ${rowsHtml}
            </ul>
            <div class="p-3 border-t border-gray-200 flex justify-end">
                <button id="bulk-skip-modal-done" class="px-4 py-2 bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-lg text-sm">Done</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);

    const close = () => modal.remove();
    document.getElementById('bulk-skip-modal-close').addEventListener('click', close);
    document.getElementById('bulk-skip-modal-done').addEventListener('click', close);

    // Outside-click closes
    modal.addEventListener('click', (e) => {
        if (e.target === modal) close();
    });

    // Escape closes
    const onKey = (e) => {
        if (e.key === 'Escape') {
            close();
            document.removeEventListener('keydown', onKey);
        }
    };
    document.addEventListener('keydown', onKey);
}

function pastTense(verb) {
    if (!verb) return 'completed';
    const v = String(verb).toLowerCase().trim();
    // Closed set — covers every bulk verb the Files tab supports.
    const map = {
        delete: 'deleted',
        deleted: 'deleted',
        move: 'moved',
        moved: 'moved',
        copy: 'copied',
        copied: 'copied',
        share: 'shared',
        shared: 'shared',
        rename: 'renamed',
        renamed: 'renamed',
        download: 'downloaded',
        downloaded: 'downloaded',
        upload: 'uploaded',
        uploaded: 'uploaded',
    };
    return map[v] || `${v}d`;
}

function escapeHtml(s) {
    return String(s)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
}
