// FX.163 p3b0 — shared "awaiting approval" notice for the gated maker-checker
// actions (GSEC FX.163, owner decision Q150=A: segregation of duties).
//
// The four gated actions — role grant, full tenant export, BYOK revoke, data-
// marking clear — answer HTTP 202 {"status":"awaiting_approval","approval_id":N}
// from their own routes while the switch is ON (CORE/API/routers/rbac.py:199,
// CORE/API/routers/pending_approvals.py:16). The Dashboard's ONE central fetch
// helper (js/api.js's fetchApi) does NOT throw on a 2xx: `if (!response.ok)`
// (js/api.js:210) skips the ApiError path for 202 and the call just RETURNS the
// parsed JSON body, so every caller sees the plain {status, approval_id} object
// and nothing tells the user the action did not run. This module is the shared
// half every wiring job (p3b1–p3b4) calls: detection + an accessible notice.
//
//   isAwaitingApproval(resp)
//       true for a 202 / body.status === 'awaiting_approval'. `resp` is either
//       the parsed body fetchApi returned, or a {status, body} envelope.
//   showAwaitingApproval(container, resp, {actionLabel})
//       renders role="note" + aria-live="polite":
//         "<actionLabel> needs a second approver. Request #<id> is waiting in
//          Security › Pending approvals" + a link to #pending-approvals
//       (js/ui.js:425 routes the hash '#pending-approvals' to the Security
//        hub's pending-approvals page). Returns the notice element so a caller
//        can keep a handle, or null when there is nothing to show.
//   clearAwaitingApproval(container)
//       removes the notice — call it on the retry path once the second
//       approver has decided and the action really runs.
//
// There is no toast/notice helper in js/utils.js (its exports are parseJwt /
// isPlatformAdmin / runInjectedScripts / withButtonBusy / optionalPageModule),
// so this module owns its own inert DOM building. Every server-supplied value
// (approval_id, the body.status echo) reaches the DOM through textContent and
// setAttribute ONLY — never innerHTML — so a hostile id cannot smuggle markup.
// tests/approval-pending.test.js runs this against a stub DOM that has NO
// innerHTML at all, which makes any markup-building here throw at test time.

export function approvalIdOf(resp) {
    if (!resp || typeof resp !== 'object') return null;
    const body = 'body' in resp ? resp.body : resp;
    if (!body || typeof body !== 'object') return null;
    const id = body.approval_id;
    return (id === 0 || id) ? id : null;
}

export function isAwaitingApproval(resp) {
    if (!resp) return false;
    // fetchApi returns the parsed BODY, so the body check is the common path;
    // the status check covers callers that hand us the raw {status, body} pair.
    if (typeof resp !== 'object') return false;
    if (resp.status === 202) return true;
    const body = (resp && 'body' in resp) ? resp.body : resp;
    return !!body && typeof body === 'object' && body.status === 'awaiting_approval';
}

// The one line the user reads. Built as plain strings, then handed to
// textContent — the em dash matches the Dashboard's existing breadcrumb glyph.
export function awaitingApprovalText(actionLabel, approvalId) {
    const label = (actionLabel && String(actionLabel).trim()) || 'This action';
    const id = (approvalId === 0 || approvalId) ? String(approvalId) : null;
    const which = id !== null ? `Request #${id}` : 'This request';
    return `${label} needs a second approver. ${which} is waiting in Security › Pending approvals`;
}

export function clearAwaitingApproval(container) {
    if (!container) return;
    const existing = container.querySelector('#awaiting-approval-notice');
    if (existing) existing.remove();
}

export function showAwaitingApproval(container, resp, opts = {}) {
    if (!container) return null;
    if (!isAwaitingApproval(resp)) return null;

    const actionLabel = opts && opts.actionLabel;
    const approvalId = approvalIdOf(resp);
    const text = awaitingApprovalText(actionLabel, approvalId);

    // Idempotent: a repeat click replaces the previous notice rather than
    // stacking duplicates under it.
    clearAwaitingApproval(container);

    const notice = document.createElement('div');
    notice.setAttribute('id', 'awaiting-approval-notice');
    notice.setAttribute('role', 'note');
    notice.setAttribute('aria-live', 'polite');
    // aria-label goes through setAttribute, so the browser serialises it as a
    // quoted attribute value — server text cannot break out of it.
    notice.setAttribute('aria-label', text);

    const line = document.createElement('p');
    line.textContent = text;
    notice.appendChild(line);

    const link = document.createElement('a');
    link.setAttribute('href', '#pending-approvals');
    link.textContent = 'Open Pending approvals';
    notice.appendChild(link);

    container.appendChild(notice);
    return notice;
}
