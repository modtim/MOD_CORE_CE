// Dashboard/js/bulk-aggregator.js
//
// data-files-launch-bulletproof §1.2 — wraps a single-item async action in
// a per-item result collector. Used by every bulk operation on the Files
// tab (delete, move, copy, share, download) so partial-failure paths
// surface as a structured `{succeeded, skipped}` outcome instead of
// silently swallowing per-item errors.
//
// Why client-side: cogs.py routes already authorize per-item; aggregating
// here keeps the API surface lean (no new bulk endpoints) and the audit
// trail granular (one audit event per actual operation). See the change's
// design.md D2 + the security walk-through.

/**
 * @typedef {Object} BulkResult
 * @property {Array<{id, name}>} succeeded — items the action completed for
 * @property {Array<{id, name, reason}>} skipped — items the action could
 *   not complete with `reason` from the closed enum (see REASON_CODES below)
 */

// Closed enum — matches the dashboard-side reason → human copy mapping in
// bulk-skip-modal.js. Adding a new reason requires updating BOTH.
export const REASON_CODES = Object.freeze({
    NOT_OWNER: 'not_owner',
    NO_PERMISSION: 'no_permission',
    SYSTEM_GENERATED: 'system_generated',
    AUDIT_LOCKED: 'audit_locked',
    NOT_FOUND: 'not_found',
    NAME_COLLISION: 'name_collision',
    QUOTA_EXCEEDED: 'quota_exceeded',
    UNKNOWN: 'unknown',
});

/**
 * Map an HTTP error response to a closed-enum reason. Tries to extract
 * meaning from the API's `detail` string; falls back to `unknown`.
 * @param {number} status HTTP status code
 * @param {string|object} detailOrBody Either parsed JSON body or raw text
 * @returns {string} a REASON_CODES value
 */
export function mapErrorToReason(status, detailOrBody) {
    let detail = '';
    if (typeof detailOrBody === 'string') {
        detail = detailOrBody;
    } else if (detailOrBody && typeof detailOrBody === 'object') {
        detail = String(detailOrBody.detail || detailOrBody.message || '');
    }
    const lower = detail.toLowerCase();

    if (status === 404) return REASON_CODES.NOT_FOUND;
    if (status === 409) {
        // Move/rename collisions return a structured 409. Treat any 409
        // as a name collision for the bulk-aggregator path; per-row
        // single-item flows that need the structured detail can read it
        // directly.
        return REASON_CODES.NAME_COLLISION;
    }
    if (status === 413) return REASON_CODES.QUOTA_EXCEEDED;
    if (status === 403) {
        // The cogs.py 403 detail strings are stable; key on substring
        // matches in priority order. Most specific first.
        if (lower.includes('system') && lower.includes('folder')) {
            return REASON_CODES.SYSTEM_GENERATED;
        }
        if (lower.includes('audit')) return REASON_CODES.AUDIT_LOCKED;
        if (lower.includes('owner') || lower.includes('not the owner')) {
            return REASON_CODES.NOT_OWNER;
        }
        // Generic permission failure
        return REASON_CODES.NO_PERMISSION;
    }
    return REASON_CODES.UNKNOWN;
}

/**
 * Run a bulk action — invoke `action(item)` for each item and aggregate
 * the per-item results. Errors are caught per-item; the bulk run never
 * raises (so the UI can render the structured outcome regardless).
 *
 * @param {Object} options
 * @param {Array<{id, name}>} options.items items to act on
 * @param {(item) => Promise<void>} options.action async function that
 *   performs the single-item operation. Throws on failure (the thrown
 *   error should expose `.status` and either `.detail` or `.body` for
 *   `mapErrorToReason` to read).
 * @returns {Promise<BulkResult>}
 */
export async function runBulkAction({ items, action }) {
    const succeeded = [];
    const skipped = [];

    for (const item of items) {
        try {
            await action(item);
            succeeded.push({ id: item.id, name: item.name });
        } catch (err) {
            const status =
                (err && (err.status || err.statusCode)) || 0;
            const detail =
                (err && (err.detail || err.body || err.message)) || '';
            const reason = mapErrorToReason(status, detail);
            skipped.push({ id: item.id, name: item.name, reason });
        }
    }

    return { succeeded, skipped };
}

/**
 * Convenience wrapper around `fetch` that throws a structured error on
 * non-2xx responses, suitable for `runBulkAction`'s error mapping. Use
 * this in `action` callbacks if you don't already have a wrapper.
 */
export async function bulkActionFetch(url, init) {
    const resp = await fetch(url, init);
    if (resp.ok) return resp;
    let body = null;
    try {
        body = await resp.json();
    } catch {
        try {
            body = await resp.text();
        } catch {
            body = null;
        }
    }
    const err = new Error(`HTTP ${resp.status}`);
    err.status = resp.status;
    err.body = body;
    err.detail = body && typeof body === 'object' ? body.detail : body;
    throw err;
}
