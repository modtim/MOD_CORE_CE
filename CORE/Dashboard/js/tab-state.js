// tab-state.js
// Persist and restore the active sub-tab on the tabbed dashboard pages
// (System, Logs, Settings, …) so a refresh (F5) returns the user to the
// exact tab they were on rather than the page's default tab (#20).
//
// The active tab lives in the URL query string (?tab=<key>), updated in
// place via history.replaceState so it survives a reload and can be
// bookmarked / shared. Top-level sidebar navigation strips the param (see
// ui.js) so switching to a different page still lands on that page's
// default tab — only an actual refresh restores the sub-tab.

// Record the active sub-tab in the URL without triggering a navigation.
export function setActiveTab(tabKey) {
    if (!tabKey) return;
    try {
        const url = new URL(window.location.href);
        if (url.searchParams.get('tab') === tabKey) return; // no-op
        url.searchParams.set('tab', tabKey);
        window.history.replaceState(null, '', url.pathname + url.search + url.hash);
    } catch (_) { /* non-fatal */ }
}

// Read the active sub-tab requested in the URL, if any.
export function getActiveTab() {
    try {
        return new URLSearchParams(window.location.search).get('tab');
    } catch (_) {
        return null;
    }
}

// Drop the ?tab= (and nested ?sub=, e.g. System → Settings → GPU Pool)
// params from the URL (used when navigating between top-level pages so a
// stale sub-tab doesn't leak onto the new page — a stray ?sub= left behind
// by clearing only ?tab= was landing unrelated hubs on the wrong sub-tab).
export function clearActiveTab() {
    try {
        const url = new URL(window.location.href);
        if (!url.searchParams.has('tab') && !url.searchParams.has('sub')) return;
        url.searchParams.delete('tab');
        url.searchParams.delete('sub');
        window.history.replaceState(null, '', url.pathname + url.search + url.hash);
    } catch (_) { /* non-fatal */ }
}
