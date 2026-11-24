// Dashboard/js/version.js
// FU10-SHOW-PRODUCT-VERSION-IN-DASHBOARD (lane dell, 11-23)
// Owner Tim: "we need to have an update #" - the release number was only in
// package.json and the downloads.html badge; a user running the CE Dashboard
// could not see which release they are on (the API reports its own 2.0.0 app
// version, and /v1/version does not exist).
//
// Single source of truth for the Dashboard's visible version. It MUST equal
// package.json "version"; CORE/API/tests/test_dashboard_product_version_visible.py
// pins the two so they cannot drift. Shared code: the same file ships in paid
// builds, which is why the rendered label is the bare `v<version>` (no hard-coded
// edition word) - the edition badge already comes from the existing edition
// logic (js/license-widget.js / js/api.js isPaidEdition).
// Do NOT confuse this with the worker agent versions (CURRENT_AGENT_VERSION etc.)
// or the API app version: this constant is the product/release number only.

export const PRODUCT_VERSION = '1.0.2';
