// Dashboard/js/licence-review.js — CE no-op stub (FIX-CE-LICENCE-REVIEW-STUB, 11-11).
//
// The real CORE/Dashboard/js/licence-review.js is denied in
// open-core/manifest.yaml. The build copies this overlay file over the denied
// path so that the static `import { initializeLicenceReviewPage }` in
// Dashboard/js/hub-compliance.js and Dashboard/js/ui.js still resolves on CE.
//
// Same export name as the real module. Does nothing: no DOM writes, no fetch
// calls, no network. Every page that would call it is hidden on CE.

export async function initializeLicenceReviewPage() {
    return undefined;
}
