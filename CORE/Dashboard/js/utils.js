export function parseJwt (token) {
    var base64Url = token.split('.')[1];
    var base64 = base64Url.replace(/-/g, '+').replace(/_/g, '/');
    var jsonPayload = decodeURIComponent(window.atob(base64).split('').map(function(c) {
        return '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2);
    }).join(''));

    return JSON.parse(jsonPayload);
}

// Single source of truth for "is this user a platform admin?".
// Used to gate UI surfaces whose backing API endpoints require
// platform-admin authorization. Pure visibility helper — the API is
// still the security boundary; unhiding a button via DevTools just
// yields a 403. Distinct from ui.js's `isSuperAdmin` (= tenant owner).
export function isPlatformAdmin(user) {
    return Boolean(user && user.is_platform_admin);
}

// --- Busy-button paradigm (async-button-feedback) -------------------------
// Shared "busy button" helper. Wraps an async action tied to a clickable
// element: on invoke it guards re-entrancy (a rapid double-click fires the
// action ONCE), disables the button, swaps in an inline spinner + a busy
// label, and always restores the original label + enabled state when the
// action settles — on success OR error. This is the same double-submit
// lockout the API-key create modal uses, factored out so every long-running
// action button (audit export, data export, …) gets consistent feedback.
//
// The spinner is an inline SVG (Tailwind's `animate-spin`), so there is no
// external asset to fetch — it works offline / air-gapped like the rest of
// the vendored assets.
//
//   withButtonBusy(btn, async () => { await doWork(); }, { busyLabel: 'Exporting…' });
//
// btn      – the HTMLElement to lock (button or anchor). null-safe.
// action   – async (or sync) function to run; its resolved value is returned.
// opts     – { busyLabel?: string }  (default 'Processing…')
// --- Script re-execution for innerHTML-mounted fragments ------------------
// Per the HTML spec, a <script> element (inline OR external, classic OR
// type="module") that arrives via `element.innerHTML = html` is parsed into
// the DOM but its "already started" flag is set during fragment parsing, so
// the browser never executes it — silently, with no console error. Several
// admin panels in this app fetch an HTML fragment and mount it with
// innerHTML (billing.js's loadFreeTrialsPanel/loadGeographicPricingPanel/
// loadSeasonalPricingPanel/loadPromoCodesPanel, ui.js's navigateToPage for
// hash-routed pages); each one that still ships its own auto-init inline
// <script> for interactivity had that script silently go dead. The same
// bug already hit pages/payment-settings.html and pages/pricing-config.html
// before those were converted to explicit JS modules (see billing.js /
// ui.js's initializePricingConfigPage() and initializePaymentSettingsPage()
// calls) — this helper generalizes that fix for panels whose inline script
// doesn't need to become a full standalone module.
//
// Call it immediately after setting `containerEl.innerHTML = html`. It walks
// every <script> under containerEl and replaces it with a freshly created
// <script> (same type/src/attributes/textContent) inserted via a real DOM
// mutation, which DOES execute per spec.
//
// Caveat: an inline `type="module"` script's relative import specifiers
// resolve against the CURRENT document's base URL, not the fragment's
// original file path. That's fine here (this Dashboard is served from its
// own subdomain root, so `../js/x.js` collapses to the correct `/js/x.js`
// either way) — but if a panel's inline module ever fails to resolve its
// imports after this, don't fight it: extract that panel's logic into a
// real `js/<panel>.js` module with an exported `initializeXPage()` and call
// it explicitly after mount, the way pricing-config.js/payment-settings.js
// already do.
export function runInjectedScripts(containerEl) {
    if (!containerEl) return;
    containerEl.querySelectorAll('script').forEach((oldScript) => {
        const newScript = document.createElement('script');
        for (const attr of oldScript.attributes) {
            newScript.setAttribute(attr.name, attr.value);
        }
        newScript.textContent = oldScript.textContent;
        oldScript.replaceWith(newScript);
    });
}

export async function withButtonBusy(btn, action, opts = {}) {
    if (typeof action !== 'function') return undefined;
    if (!btn) return action();
    // Re-entrancy / double-submit guard: if already busy, ignore the click.
    if (btn.dataset.modBusy === '1') return undefined;

    const busyLabel = opts.busyLabel || 'Processing…';
    const originalHtml = btn.innerHTML;
    const originalDisabled = btn.disabled;

    btn.dataset.modBusy = '1';
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    // Preserve display sizing; label text is developer-provided (not user
    // input), so a text node keeps it inert regardless.
    const spinner = '<svg class="animate-spin h-4 w-4 inline-block align-[-2px]" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 0 1 8-8V0C5.4 0 0 5.4 0 12h4z"></path></svg>';
    const label = document.createElement('span');
    label.textContent = busyLabel;
    btn.innerHTML = '<span class="inline-flex items-center gap-2">' + spinner + label.outerHTML + '</span>';

    try {
        return await action();
    } finally {
        delete btn.dataset.modBusy;
        btn.disabled = originalDisabled;
        btn.removeAttribute('aria-busy');
        btn.innerHTML = originalHtml;
    }
}

// FX.162.p6 — guarded dynamic import for page modules the open-core CE carve
// denies (open-core/manifest.yaml: CORE/Dashboard/js/compliance*.js,
// security-lockdown.js, outbound-connections.js). A static `import ... from`
// of a denied file breaks the WHOLE Dashboard module graph on a CE build, so
// every such page is loaded through this helper instead: the module is fetched
// at the moment its initializer is about to run, a missing module (CE carve)
// resolves to null, and the call site skips the initializer. Paid builds get
// the exact same module object the static import used to yield, so the
// initializer runs with the same arguments in the same order.
export async function optionalPageModule(path, exportName) {
    try {
        const mod = await import(path);
        return (mod || {})[exportName] || null;
    } catch (err) {
        return null;
    }
}

// --- Shared copy helper (FIX-DASHBOARD-SHARED-COPY-HELPER, 11-17) BEGIN
// Every Dashboard Copy button must work over plain http on the LAN (first-class
// CE LAN access): navigator.clipboard is only exposed in secure contexts (https
// / localhost), so a direct writeText throws on http://<lan-ip> and the button
// fails silently. One shared helper for every copy surface: the async Clipboard
// API when the page is a secure context and writeText exists, else the hidden
// textarea + document.execCommand('copy') idiom the Logs page already ships.
// Returns a promise of true/false. When a button element is passed it also
// applies the standard visible feedback ('Copied!' ~2s, or 'Copy failed: select
// and copy manually' ~6s) - a copy button never fails silently. What is copied
// is byte-identical to what the call site passes.
export async function modCopyText(text, buttonEl) {
    let ok = false;
    try {
        if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
            ok = true;
        }
    } catch (_) { /* fall through to the legacy path */ }
    if (!ok) {
        try {
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            ok = !!document.execCommand('copy');
            document.body.removeChild(ta);
        } catch (_) { ok = false; }
    }
    if (buttonEl) {
        const original = buttonEl.textContent;
        buttonEl.textContent = ok ? 'Copied!' : 'Copy failed: select and copy manually';
        setTimeout(() => { buttonEl.textContent = original; }, ok ? 2000 : 6000);
    }
    return ok;
}
window.modCopyText = modCopyText;
// FIX-DASHBOARD-SHARED-COPY-HELPER END
