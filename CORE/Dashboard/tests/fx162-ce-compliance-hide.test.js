// Node-runnable check for the FX.162.p3 CE compliance hiding in
// Dashboard/js/ui.js (helpers: _CE_COMPLIANCE_PAGES + _isCeEdition +
// _hideCeComplianceNav + _ceCompliancePanelHtml, all added by p3).
//
// ui.js is a large ES module, so (like security-notice-banner.test.js) we
// slice out just the self-contained p3 helper block, run it in a vm sandbox
// with a stubbed getPlatformFeatures + a tiny DOM, and assert:
//   * edition 'ce' hides ALL FIVE compliance nav links (one helper);
//   * edition 'pro'/'saas' hides NOTHING (paid builds keep the pages);
//   * an unreadable /platform/features payload hides ALL FIVE (fail closed, rc20 failopen-a);
//   * the direct-URL panel says "Not available in MOD Community Edition";
//   * the panel is static markup (no page content can inject into it).
//
//   node --test CORE/Dashboard/tests/fx162-ce-compliance-hide.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'ui.js'), 'utf8');

const fnStart = src.indexOf('const _CE_COMPLIANCE_PAGES = new Set([');
assert.ok(fnStart >= 0, '_CE_COMPLIANCE_PAGES not found in ui.js');
const marker = src.indexOf('export async function navigateToPage(hash) {', fnStart);
assert.ok(marker > fnStart, 'end marker for the p3 helper block not found');
const snippet = src.slice(fnStart, marker);

const PAGES = ['compliance', 'auditor-access', 'parental-consent',
    'security-lockdown', 'outbound-connections'];

function makeLinks() {
    const links = {};
    for (const p of PAGES) {
        links[`${p}-nav-link`] = { id: `${p}-nav-link`, style: { display: 'flex' } };
    }
    return links;
}

// Run the helper block + one _hideCeComplianceNav() call, then let the
// promise chain settle (getPlatformFeatures -> .then -> hide loop).
async function hidden(featuresResult) {
    const links = makeLinks();
    const sandbox = {
        // querySelectorAll stub: _hideCeComplianceNav ends with
        // _syncNavGroupHeadings(), which walks .nav-group divs — the sandbox
        // has none, so an empty list keeps that tail a no-op.
        document: { getElementById: (id) => (links[id] || null), querySelectorAll: () => [] },
        getPlatformFeatures: featuresResult,
        console,
    };
    vm.runInNewContext(
        `"use strict";\n${snippet}\n_hideCeComplianceNav();`,
        sandbox,
    );
    for (let i = 0; i < 5; i++) await Promise.resolve();
    return links;
}

function panel() {
    const sandbox = { console };
    return vm.runInNewContext(
        `"use strict";\n${snippet}\n_ceCompliancePanelHtml();`, sandbox);
}

test.describe('FX.162.p3 — CE hides the compliance nav + pages');

test('edition=ce hides all five compliance nav links', async () => {
    const links = await hidden(async () => ({ entitlements: { edition: 'ce' } }));
    for (const p of PAGES) {
        assert.strictEqual(links[`${p}-nav-link`].style.display, 'none',
            `${p} nav must be hidden on CE`);
    }
});

test('edition=pro hides nothing', async () => {
    const links = await hidden(async () => ({ entitlements: { edition: 'pro' } }));
    for (const p of PAGES) {
        assert.strictEqual(links[`${p}-nav-link`].style.display, 'flex',
            `${p} nav must stay visible on pro`);
    }
});

test('edition=saas hides nothing', async () => {
    const links = await hidden(async () => ({ entitlements: { edition: 'saas' } }));
    for (const p of PAGES) {
        assert.strictEqual(links[`${p}-nav-link`].style.display, 'flex',
            `${p} nav must stay visible on saas`);
    }
});

test('unreadable payload (offline/500) hides all five — fail closed', async () => {
    const links = await hidden(async () => { throw new Error('HTTP 500'); });
    for (const p of PAGES) {
        assert.strictEqual(links[`${p}-nav-link`].style.display, 'none',
            'unknown edition must never reveal a CE-denied surface');
    }
});

test('missing entitlements block hides all five (fail closed)', async () => {
    const links = await hidden(async () => ({ deployment_mode: 'onprem' }));
    for (const p of PAGES) {
        assert.strictEqual(links[`${p}-nav-link`].style.display, 'none',
            'no edition info -> assume CE, hide');
    }
});

test('entitlements FAILURE stub (edition ce + error) hides all five', async () => {
    // main.py's failure stub reports edition "ce" even on a paid on-prem
    // image, so an errored block is unknown, which now counts as CE (fail
    // closed): the compliance nav must be hidden, never revealed.
    const links = await hidden(async () => ({ entitlements: { edition: 'ce', error: 'unresolved' } }));
    for (const p of PAGES) {
        assert.strictEqual(links[`${p}-nav-link`].style.display, 'none',
            'errored entitlements must hide compliance (fail closed)');
    }
});

test('direct URL panel names the edition', async () => {
    const html = panel();
    assert.ok(html.includes('Not available in MOD Community Edition'),
        'panel must carry the exact owner wording');
    assert.ok(html.includes('bg-amber-100'), 'panel is a short amber notice');
});

test('panel is static markup (nothing injectable)', async () => {
    const html = panel();
    assert.ok(!/\$\{/.test(html), 'no template interpolation in the panel');
    assert.ok(!/<script/i.test(html), 'no script tag in the panel');
});
