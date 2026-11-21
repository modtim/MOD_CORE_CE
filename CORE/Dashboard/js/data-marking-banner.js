// FX.79 p3a — CUI/ITAR data-marking banner for the Dashboard's asset
// preview panel (the single-asset view: pages/files.html).
//
// Wording + label ranking MIRROR `CORE/API/services/marking_stamps.py`
// (FX.79 p3b, the canonical export stamp) exactly like the
// WorkflowBuilder's lib/data-markings.ts does: worst-ranked label wins,
// the dissemination addendum joins as LABEL/DISSEMINATION, and the banner
// is `CONTROLLED: <HEADER> — handle per policy` (em dash) so screen and
// exported MARKING.txt agree word-for-word.
//
// Read path: GET {api}/tenant/data-markings/{resource_type}/{resource_id}
// (CORE/API/routers/tenant/data_markings.py, mounted at /v1/tenant).
//
// Behaviour contract (same as the WB banner):
//   - no live marking / CE carve / 403 / fetch failure → nothing renders,
//     silently — the preview is never blocked or spammed with console
//     errors;
//   - the banner is NOT dismissible, carries role="note" + aria-label,
//     and keeps a bg-red-900/text-red-50 treatment (contrast ≥ 4.5:1).

// FX.163 p3b4a — the "Clear" button is maker-checker gated server-side
// (CORE/API/routers/tenant/data_markings.py:331, DELETE
// /v1/tenant/data-markings/{resource_type}/{resource_id}/{label}, gated by
// approval_gate.gate_data_marking_clear under owner decision Q150=A). While
// the second-approver switch is ON the DELETE answers 202
// {status:'awaiting_approval', approval_id} and the mark is STILL live, so
// the banner must keep showing it plus the shared pending notice.
// (The import sits above the module's API_URL constant — the test harness
// slices the body from there, so the vm sandbox supplies these three
// helpers as globals.)
import { isAwaitingApproval, showAwaitingApproval, clearAwaitingApproval } from './approval-pending.js';
import { isPaidEdition } from './api.js';

const API_BASE_URL = window.APP_CONFIG?.API_URL || "http://api.localhost";

// Same values as marking_stamps.LABEL_RANK (keep in lock-step).
export const LABEL_RANK = {
    ITAR: 90,
    "CUI//SP-EXPT": 80,
    CUI: 70,
    EXPORT_CONTROLLED: 60,
    EAR99: 55,
    PROPRIETARY: 50,
};
export const CUSTOM_RANK = 40;

export function rankLabel(code) {
    return LABEL_RANK[String(code).toUpperCase()] ?? CUSTOM_RANK;
}

// Fold a resource's live marking rows into ONE stamp — mirrors
// marking_stamps.export_stamp (sort by (-rank, label code), winner first).
export function worstMarking(rows) {
    const ranked = [];
    for (const row of rows ?? []) {
        const code = String(row?.label ?? '').trim();
        if (!code) continue;
        ranked.push([rankLabel(code), code, String(row?.dissemination ?? '').trim()]);
    }
    if (!ranked.length) return null;
    ranked.sort((a, b) => (b[0] - a[0]) || (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0));
    const [, label, dissemination] = ranked[0];
    return {
        label,
        dissemination: dissemination || null,
        header: dissemination ? `${label}/${dissemination}` : label,
        labels: [...new Set(ranked.map((t) => t[1]))].sort(),
    };
}

// The canonical UI banner sentence (word-for-word marking_stamps.py).
export function markingBannerText(stamp) {
    return `CONTROLLED: ${stamp.header} — handle per policy`;
}

// Fetch the live marks for one resource → stamp (null when nothing
// applies). Silent on every failure mode — this is a background nicety
// in the preview panel, not a gate.
export async function fetchMarkingStamp(resourceType, resourceId) {
    try {
        const token = sessionStorage.getItem('accessToken');
        if (!token) return null;
        // CE carve: /v1/tenant/data-markings is not mounted on CE (routers/tenant
        // is denylisted in open-core/manifest.yaml), so skip the call instead of
        // firing a 404 under /v1/ that the e2e net-guard flags as a product
        // defect. Fail-open: only a POSITIVE edition "ce" report skips (same
        // predicate as ui.js _isCeEdition / files.js loadStorageUsage). The
        // `typeof` guard keeps the unit-test sandbox (tests/data-marking-banner.
        // test.js slices this body from the API_BASE_URL line, so the import
        // binding is absent there) a no-op — paid behaviour + timing unchanged.
        if (typeof isPaidEdition === 'function' && !(await isPaidEdition())) return null;
        const resp = await fetch(
            `${API_BASE_URL}/tenant/data-markings/${encodeURIComponent(resourceType)}/${encodeURIComponent(String(resourceId))}`,
            { headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' } },
        );
        if (!resp.ok) return null; // CE carve / no capability / unmarked
        const data = await resp.json();
        const rows = Array.isArray(data) ? data : (data?.items ?? []);
        return worstMarking(rows);
    } catch {
        return null;
    }
}

function _escapeHtml(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

const BANNER_ID = 'asset-marking-banner';
// FX.163 p3b4a — the per-label "Clear" button on the banner. Class + id prefix
// are the only hooks the click wiring and the tests use.
const CLEAR_BUTTON_CLASS = 'asset-marking-clear-button';
const CLEAR_ERROR_CLASS = 'asset-marking-clear-error';

// Render (or clear) the banner at the top of the asset preview panel.
export function renderAssetMarkingBanner(stamp, opts = {}) {
    const el = document.getElementById(BANNER_ID);
    if (!el) return;
    const canClear = !!opts && opts.canClear === true;
    if (!stamp) {
        el.removeAttribute('role');
        el.removeAttribute('aria-label');
        el.innerHTML = '';
        el.className = '';
        return;
    }
    const text = markingBannerText(stamp);
    el.setAttribute('role', 'note');
    el.setAttribute('aria-label', text);
    el.className =
        'w-full bg-red-900 text-red-50 border-b border-red-950 px-4 py-2 text-sm font-semibold text-center';
    // text only — the label code comes from the admin label catalogue,
    // but escape anyway so a hostile code can never inject markup.
    el.innerHTML = _escapeHtml(text);
    // FX.163 p3b4a — one "Clear" button per label the banner carries, tenant
    // admins / superadmins only (the caller passes canClear; default false).
    if (!canClear) return;
    for (const label of (stamp.labels ?? [])) {
        const button = document.createElement('button');
        button.setAttribute('class', CLEAR_BUTTON_CLASS);
        button.setAttribute('data-label', label);
        button.setAttribute('type', 'button');
        button.textContent = 'Clear';
        el.appendChild(button);
    }
}

export function clearAssetMarkingBanner() {
    renderAssetMarkingBanner(null);
}

// Load the marking for one asset (cog) into the preview banner, guarded
// against the user switching files mid-flight (only the previewed cog's
// result is applied). `canClear` (FX.163 p3b4a) is the caller's tenant-
// admin / superadmin verdict — default false, so a caller that does not
// know simply never sees the "Clear" button.
export async function loadAssetMarkingBanner(cogId, isCurrent, canClear = false) {
    const stamp = await fetchMarkingStamp('asset', cogId);
    if (!isCurrent()) return; // a different asset is previewed now
    renderAssetMarkingBanner(stamp, { canClear });
    bindAssetMarkingClearButtons('asset', cogId, isCurrent, canClear);
}

// FX.163 p3b4a — the banner's per-label "Clear" buttons. The DELETE route
// soft-deletes ONE label (audit-logged); the banner re-renders from a fresh
// GET, so whatever survives (the other labels, or — under the second-approver
// switch — the very label the approver has not cleared yet) is what the user
// sees. Every server-supplied string reaches the DOM through textContent
// only; there is no innerHTML anywhere in this path.
export async function clearAssetMarkingLabel(resourceType, resourceId, label, opts = {}) {
    const el = document.getElementById(BANNER_ID);
    if (!el) return null;
    if (!confirm(`Clear the data marking "${label}" on this asset?`)) return null;
    // Inline error slot: removed on the next success, never left over.
    const staleError = el.querySelector('.' + CLEAR_ERROR_CLASS);
    if (staleError) staleError.remove();
    try {
        const token = sessionStorage.getItem('accessToken');
        if (!token) throw new Error('not signed in');
        const resp = await fetch(
            `${API_BASE_URL}/tenant/data-markings/${encodeURIComponent(resourceType)}/${encodeURIComponent(String(resourceId))}/${encodeURIComponent(String(label))}`,
            {
                method: 'DELETE',
                headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
            },
        );
        // Unlike fetchApi, raw fetch hands us the RESPONSE, so the 202's
        // {status, approval_id} body has to be parsed before the helpers see
        // it — the envelope shape they accept is {status, body}.
        let payload = null;
        try { payload = await resp.json(); } catch { /* 204 has no body */ }
        if (isAwaitingApproval(resp) || isAwaitingApproval(payload)) {
            // GSEC FX.163 (Q150=A): the clear did NOT run — a second approver
            // has to approve it. Keep the marking shown, add the shared notice.
            showAwaitingApproval(el, { status: resp.status, body: payload }, { actionLabel: 'Clear data marking' });
            return { awaiting: true };
        }
        if (!resp.ok) {
            const err = document.createElement('p');
            err.setAttribute('class', CLEAR_ERROR_CLASS);
            err.textContent = `Could not clear "${label}" — the server rejected the request.`;
            el.appendChild(err);
            return { ok: false };
        }
    } catch (e) {
        const err = document.createElement('p');
        err.setAttribute('class', CLEAR_ERROR_CLASS);
        err.textContent = `Could not clear "${label}": ${e?.message || 'Unknown error'}`;
        el.appendChild(err);
        return { ok: false };
    }
    clearAwaitingApproval(el);
    return { ok: true };
}

// Re-fetch + re-render after a clear (or after the second approver acts on a
// pending request). Same file-switch guard as loadAssetMarkingBanner.
export async function refreshAssetMarkingBanner(resourceType, resourceId, isCurrent, canClear = false) {
    const stamp = await fetchMarkingStamp(resourceType, resourceId);
    if (!isCurrent()) return; // a different asset is previewed now
    renderAssetMarkingBanner(stamp, { canClear });
    // the re-render drew fresh buttons — they need their click handlers back
    bindAssetMarkingClearButtons(resourceType, resourceId, isCurrent, canClear);
}

// Wire the click handlers onto the buttons renderAssetMarkingBanner just drew.
// One handler per button (not one delegated handler) so a label that goes
// stale after a file switch can never clear the wrong asset.
export function bindAssetMarkingClearButtons(resourceType, resourceId, isCurrent, canClear = false) {
    const el = document.getElementById(BANNER_ID);
    if (!el) return;
    for (const button of el.querySelectorAll('.' + CLEAR_BUTTON_CLASS)) {
        const label = button.getAttribute('data-label');
        if (!label) continue;
        button.addEventListener('click', async () => {
            const outcome = await clearAssetMarkingLabel(resourceType, resourceId, label, { confirm: true });
            if (outcome && outcome.awaiting) return; // marking stays shown
            if (outcome && outcome.ok) await refreshAssetMarkingBanner(resourceType, resourceId, isCurrent, canClear);
        });
    }
}
