// Dashboard/js/runbook-links.js
//
// FX.160 p5 (owner R36.5 = A) — "Runbook" links on the matching Dashboard
// admin pages. Runbooks are published (sanitized) on the docs-site, so the
// admin pages link to the docs-site pages — never to the internal
// docs/runbooks/ tree, which is not shipped in the public docs-site build.
//
// Same URL source as js/help.js: window.APP_CONFIG.DOCS_URL (set by
// js/config.js), with the same public fallback. No hard-coded hostnames in
// the anchors themselves, no new env vars.
//
// Each anchor lives in the page that performs the operation its runbook
// describes (System page panels, Secrets, SSO, Key Inventory). hrefs are
// filled at first paint by _loadRunbookLinks(), called from ui.js right
// after the page HTML is inserted — before the page's own initializer runs,
// so a page module that re-renders its header can never clobber the link.
// Anchors are static in the baked HTML, so they survive a rebuild without
// this module; the module only supplies the live docs URL.

export async function _loadRunbookLinks() {
    const docsBase = (window.APP_CONFIG && window.APP_CONFIG.DOCS_URL) || 'https://example.com';

    // Only link docs-site pages that really exist (verified against the
    // front-matter slugs in CORE/docs-site/docs). The tracked operator runbooks
    // below are the sanitized public pages under CORE/docs-site/docs/runbooks/
    // (FX.160 s1, owner R36.5 = A); the rest still point at their published
    // equivalents. keycloak-realm-change (docs/runbooks/, slug
    // /runbooks/keycloak-realm-change) is a public page with no anchor in this
    // map; rb-backups points at the security page /security/backup-and-restore.
    const links = {
        'rb-system': '/runbooks/api-restart-and-drain',
        'rb-backups': '/security/backup-and-restore',
        'rb-stacks': '/runbooks/stack-deploy-and-recover',
        'rb-workers': '/runbooks/worker-enrol-and-retire',
        'rb-secrets': '/security/vault-unseal-runbook',
        'rb-sso': '/security/identity-and-sessions',
        'rb-keys': '/runbooks/certificate-renewal',
        // FX.160 p5b — the remaining admin pages that perform the operation.
        'rb-siem': '/security/siem-hookup',
        'rb-releases': '/security/upgrade-and-rollback',
        'rb-lockbox': '/security/break-glass',
        'rb-bundle': '/security/creating-a-support-bundle',
        // FX.160 p5c — maintenance, auditor access, access reviews, audit logs.
        'rb-maintenance': '/security/restart-window-checklist',
        'rb-auditor': '/security/auditor-access',
        'rb-access-reviews': '/security/access-reviews',
        'rb-audit-logs': '/security/siem-event-schema',
        // FX.160 p5d5 — security incidents.
        'rb-incidents': '/runbooks/incident-first-steps',
    };

    Object.entries(links).forEach(([id, path]) => {
        const el = document.getElementById(id);
        if (el) el.href = docsBase + path;
    });
}
