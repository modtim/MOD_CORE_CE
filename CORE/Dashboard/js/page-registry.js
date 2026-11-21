// CE-NOPAID-4.2 leg A (11-14) — the page registry module.
//
// PAGE_REGISTRY and registerPage moved here verbatim from ui-manifest.js (no behaviour
// change). ui-manifest.js imports both names from this module and re-exports them, so
// every existing importer keeps working unchanged.
//
// Shipping: this module is plain data + helpers, no paid text. The paid rows themselves
// stay in ui-manifest.js (registerDashboardPages) and are scrubbed out of the CE extract
// by open-core/manifest.yaml, as before. Legs B-D (ui.js / api.js / hub-compliance.js)
// switch their lookups to getPageEntry / isPaidPage in later jobs.
//
// Proof: CORE/Dashboard/tests/test_page_registry.check.js.

export const PAGE_REGISTRY = [];

export function registerPage(entry) {
    PAGE_REGISTRY.push(entry);
    return entry;
}

// Returns the registry row for a page id, or undefined when the id has no row.
export function getPageEntry(id) {
    for (const entry of PAGE_REGISTRY) {
        if (entry && entry.id === id) return entry;
    }
    return undefined;
}

// True only when the id has a row AND that row is marked paid: true.
// An unknown id is NOT paid (the CE gate denies unknown ids by a separate path).
export function isPaidPage(id) {
    const entry = getPageEntry(id);
    return !!(entry && entry.paid === true);
}
