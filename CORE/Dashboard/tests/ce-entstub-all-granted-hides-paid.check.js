'use strict';
// CE-ENTSTUB-UI — Community Edition ships an entitlements stub that GRANTS every
// capability (tier_name 'community', limits all unlimited). Anything in the Dashboard
// that decided "show this paid page/button" from that payload would start showing
// pages whose API routes the CE carve does not ship. This check runs the SHIPPED
// source (extracted verbatim, no re-typed copies) against a CE /platform/features
// payload whose capability list contains EVERYTHING and proves:
//
//   E1  edition predicates (_isCeEdition / isPaidEdition) read ONLY `edition`: the
//       all-granting CE payload is CE, a hostile tier_name does not change that, and
//       paid editions stay paid
//   E2  the sidebar: after every CE hide helper runs on a worst-case DOM (every row
//       revealed by its role gate) no paid row is visible, and the result is
//       IDENTICAL for an empty capability list and for the all-granting list
//   E3  every row still visible on CE is a named CE-core row; every other row is
//       hidden unconditionally in updateUIVisibility or never revealed by any script
//   E4  the direct-URL route gates cover every paid page id (incl. the five billing
//       admin pages this change added) and cannot be reached with a CE payload
//   E5  the Security and Compliance hubs expose none of the paid tabs on CE
//   E6  the only readers of the capability list are the five MODEntitlements
//       importers; every lockElement() caller sits on a CE-gated page, and with all
//       capabilities granted lockElement leaves the control untouched
//
//   node CORE/Dashboard/tests/ce-entstub-all-granted-hides-paid.check.js
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const read = (...p) => fs.readFileSync(path.join(ROOT, ...p), 'utf8');
const exists = (...p) => fs.existsSync(path.join(ROOT, ...p));

const uiSrc = read('js', 'ui.js');
const apiSrc = read('js', 'api.js');
const shell = read('pages', 'dashboard-shell.html');

// ---------------------------------------------------------------- payloads
class AllNames extends Array { includes() { return true; } }
const CAP_NAMES = ['audit_hmac', 'byo_s3', 'custom_nodes', 'custom_registry', 'gpu_pool',
    'k8s_backend', 'k8s_multi_cluster', 'long_retention', 'marketplace_publish', 'predictive',
    'scim', 'show_timing', 'sso', 'stack_edit', 'stack_egress', 'stack_wizard', 'multi_tenant',
    'sso_federated', 'burst', 'managed_ai', 'log_streaming', 'fleet_console',
    'multi_site_federation', 'compliance_center', 'byok', 'custom_plans', 'billing'];
const UNLIMITED_LIMITS = { max_seats: 'unlimited', max_storage_gb: 'unlimited',
    credits_per_month: 'unlimited', max_concurrent_processes: 'unlimited',
    max_cpu_hours_monthly: 'unlimited', included_egress_gb: 'unlimited',
    max_manager_nodes: 'unlimited', max_worker_nodes: 'unlimited', max_tenants: 'unlimited' };

function payload(edition, caps, extra) {
    return {
        billing_enabled: false,
        entitlements: Object.assign({
            edition, tier_name: 'community', source: 'baseline',
            capabilities: caps === 'all' ? AllNames.from(CAP_NAMES) : [],
            limits: caps === 'all' ? UNLIMITED_LIMITS : {},
            compliance: { declared_environment: [], needs: [] },
        }, extra || {}),
    };
}
const CE_ALL = payload('ce', 'all');
const CE_NONE = payload('ce', 'none');
const CE_HOSTILE = payload('ce', 'all', { tier_name: 'enterprise', source: 'license' });
const PRO_ALL = payload('pro', 'all');
const SAAS_ALL = payload('saas', 'all');

// ---------------------------------------------------------------- E1 predicates
const blockStart = uiSrc.indexOf('const _CE_COMPLIANCE_PAGES = new Set([');
const blockEnd = uiSrc.indexOf('export async function navigateToPage(hash) {');
assert.ok(blockStart >= 0 && blockEnd > blockStart, 'ui.js helper block not found');
const block = uiSrc.slice(blockStart, blockEnd);

const isPaidFn = /export async function isPaidEdition\(\) \{[\s\S]*?\n\}/.exec(apiSrc);
assert.ok(isPaidFn, 'api.js isPaidEdition not found');
const isPaidSrc = isPaidFn[0].replace('export async function', 'async function');

// FIX-CE-DASH-SWEEP-C2 (liftable-shape drift): ui.js:527 _ceRetiredAdminNavIds() reads
// _ceDeniedPageIds(), which CE-NOPAID-4.1 moved out of the ui.js helper block into
// js/ui-manifest.js (ui.js:34 imports it), so the lifted block alone ReferenceErrors.
// ui-manifest.js carries no imports of its own, so lift its SHIPPED text the same way the
// ui.js block is lifted (export -> plain function) and populate PAGE_REGISTRY exactly as
// ui.js does at module load (registerDashboardPages() + registerCeGateAliases(), ui.js:40/41).
// On the paid tree this is the real registry (106 rows + aliases, 48 paid:true); on a CE cut
// the manifest scrub removes every paid:true row, which is exactly what the extract ships.
// If the module is ever absent, fall back to the CE-leg stub (zero denied ids) and say so.
const manifestPath = path.join(ROOT, 'js', 'ui-manifest.js');
// FIX-CE-ENTSTUB-LIFT-IMPORT (this job): CE-NOPAID-4.2 leg A moved PAGE_REGISTRY +
// registerPage into js/page-registry.js, so ui-manifest.js:43 now carries a real `import`
// line. The old lift stripped only `^export `, which left that import inside a classic
// `new Function` body -> "Cannot use import statement outside a module" (the check died at
// :116). Strip import lines (single-line AND multi-line `import ... from '...';` forms) and
// prepend the SAME shim page-registry.js ships (test_page_registry.check.js:14-17 verbatim),
// so the lifted body keeps populating one shared PAGE_REGISTRY array exactly as before.
const manifestRegistryShim = 'const PAGE_REGISTRY = [];\nfunction registerPage(entry) { PAGE_REGISTRY.push(entry); return entry; }\nfunction registerPageAlias(entry) { PAGE_REGISTRY.push(entry); return entry; }\n';
const manifestLift = fs.existsSync(manifestPath)
    ? manifestRegistryShim + fs.readFileSync(manifestPath, 'utf8')
        .replace(/^import[\s\S]*?from\s+'[^']+';[ \t]*$/gm, '')
        .replace(/^import .*;?[ \t]*$/gm, '')
        .replace(/^export /gm, '')
        + '\n' + (fs.existsSync(path.join(ROOT, '..', 'API', 'paid_addon', 'dashboard', 'ui-manifest-paid.js'))
            ? fs.readFileSync(path.join(ROOT, '..', 'API', 'paid_addon', 'dashboard', 'ui-manifest-paid.js'), 'utf8')
                .replace(/^export /gm, '') + '\nregisterPaidRows(registerPage);\nregisterCeGateAliases(registerPageAlias);\n'
            : '')
        + '\nregisterDashboardPages();\n'
    : (console.log('SKIP ui-manifest.js absent: _ceDeniedPageIds stubbed to the CE leg (0 ids)'),
       'function _ceDeniedPageIds() { return []; }\n');

async function isPaid(features) {
    return new Function('getPlatformFeatures', `${isPaidSrc}\nreturn isPaidEdition();`)(
        () => Promise.resolve(features));
}
async function isCe(features) {
    return new Function('document', 'getPlatformFeatures', `${block}\nreturn _isCeEdition();`)(
        { getElementById: () => null, querySelectorAll: () => [] }, () => Promise.resolve(features));
}

// ---------------------------------------------------------------- E2 sidebar
const navRows = [...shell.matchAll(/id="([a-z0-9-]*-nav-link)"/g)].map((m) => m[1]);
// CE carve (FIX-CE-DASH-SWEEP-C): the scrub removes the paid nav rows from
// dashboard-shell.html, so the paid-tree floor (20 rows) cannot hold on a CE
// cut. The floor derives from the tree: a tree that ships a carved paid module
// (js/seller.js absent) keeps the assert at the CE floor; on the paid tree it
// stays exactly 20.
const navRowsFloor = fs.existsSync(path.join(ROOT, 'js', 'seller.js')) ? 20 : 10;
assert.ok(navRows.length >= navRowsFloor, 'dashboard-shell.html nav rows not found');

async function visibleAfterHelpers(features, isSuperAdmin, isPlatformAdmin) {
    const links = {};
    for (const id of navRows) links[id] = { style: { display: 'flex' } };
    const doc = { getElementById: (id) => links[id] || null, querySelectorAll: () => [] };
    await new Function('document', 'getPlatformFeatures',
        `${manifestLift}
${block}
return Promise.all([_hideCeComplianceNav(), _hideCePaidComplianceNav(),
    _hideCeRetiredAdminNav(), _applyCePaidHubNav(${isSuperAdmin}, ${isPlatformAdmin})]);`)(
        doc, () => Promise.resolve(features));
    return navRows.filter((id) => links[id].style.display !== 'none').sort();
}

// What a CE install keeps in the sidebar: the core rows only.
const CE_CORE_ROWS = ['help-nav-link', 'logs-nav-link', 'permissions-nav-link',
    'settings-nav-link', 'system-nav-link', 'users-nav-link', 'workers-nav-link'];
// Rows updateUIVisibility hides unconditionally (consolidated into a hub / retired).
const HIDDEN_BY_OWN_BLOCK = ['admin-nodes-nav-link', 'admin-plans-nav-link', 'capture-devices-nav-link',
    'gpu-pool-nav-link', 'payments-admin-nav-link', 'pricing-nav-link', 'stack-billing-nav-link',
    'storage-nav-link', 'webhooks-nav-link'];
// Rows that ship display:none and that no script ever reveals.
const NEVER_REVEALED = ['free-trials-nav-link', 'geographic-pricing-nav-link', 'pricing-config-nav-link',
    'promo-codes-nav-link', 'seasonal-pricing-nav-link'];
const PAID_ROWS = ['marketplace-nav-link', 'security-nav-link', 'compliance-center-nav-link',
    'billing-nav-link', 'admin-billing-nav-link', 'drift-events-nav-link'];

function jsFiles() {
    return fs.readdirSync(path.join(ROOT, 'js')).filter((f) => f.endsWith('.js'));
}

(async function () {
    // E1
    assert.strictEqual(await isCe(CE_ALL), true, 'CE + every capability granted must stay CE');
    assert.strictEqual(await isPaid(CE_ALL), false, 'CE + every capability granted must not read as paid');
    assert.strictEqual(await isCe(CE_HOSTILE), true, 'tier_name / source must not change the edition');
    assert.strictEqual(await isPaid(CE_HOSTILE), false);
    assert.strictEqual(await isPaid(PRO_ALL), true, 'paid editions stay paid (pro)');
    assert.strictEqual(await isPaid(SAAS_ALL), true, 'paid editions stay paid (saas)');
    assert.strictEqual(await isCe(PRO_ALL), false);
    assert.strictEqual(await isCe(SAAS_ALL), false);
    assert.strictEqual(await isPaid({ entitlements: { edition: 'ce', error: 'unresolved' } }), false,
        'the failure stub stays fail-closed');

    // E2: worst case = every row already revealed by its role gate
    for (const [who, sa, pa] of [['platform admin', false, true], ['tenant admin', true, false],
        ['both', true, true]]) {
        const granted = await visibleAfterHelpers(CE_ALL, sa, pa);
        const none = await visibleAfterHelpers(CE_NONE, sa, pa);
        const hostile = await visibleAfterHelpers(CE_HOSTILE, sa, pa);
        assert.deepStrictEqual(granted, none,
            `${who}: the capability list must not change the CE sidebar`);
        assert.deepStrictEqual(granted, hostile, `${who}: tier_name must not change the CE sidebar`);
        for (const id of PAID_ROWS) {
            assert.ok(!granted.includes(id), `${who}: paid row ${id} visible on CE with every capability granted`);
        }
        // E3
        for (const id of granted) {
            assert.ok(CE_CORE_ROWS.includes(id) || HIDDEN_BY_OWN_BLOCK.includes(id) || NEVER_REVEALED.includes(id),
                `${who}: ${id} is visible after every CE helper and is not a named CE row: review it against the CE API`);
        }
    }
    // FIX-CE-DASH-SWEEP-C2: the CE scrub removes these paid nav ROWS from
    // dashboard-shell.html (open-core/manifest.yaml rows 4385/4388 + the
    // security/compliance hub rows), so a CE cut has no such row to keep visible and
    // the paid assert cannot hold there. Same tree-shape style as the navRowsFloor
    // guard above: on the paid tree (the rows ship) the assert stays VERBATIM; on the
    // scrubbed extract the block prints a SKIP and the CE halves above stay live.
    const paidRowsShip = shell.includes('id="marketplace-nav-link"');
    if (!paidRowsShip) {
        console.log('SKIP paid platform admin rows still visible: dashboard-shell.html scrubbed (the paid nav rows are not in this tree)');
    } else {
    const paidProRows = await visibleAfterHelpers(PRO_ALL, false, true);
    for (const id of ['marketplace-nav-link', 'security-nav-link', 'compliance-center-nav-link',
        'admin-billing-nav-link']) {
        assert.ok(paidProRows.includes(id), `paid platform admin must still see ${id} (paid behaviour unchanged)`);
    }
    }

    // E3 (static half)
    const updStart = uiSrc.indexOf('function updateUIVisibility');
    assert.ok(updStart >= 0, 'updateUIVisibility not found');
    const upd = uiSrc.slice(updStart);
    for (const id of HIDDEN_BY_OWN_BLOCK) {
        const re = new RegExp(`const (\\w+) = document\\.getElementById\\('${id}'\\);\\s*if \\(\\1\\) \\{\\s*\\1\\.style\\.display = 'none'`);
        const re2 = id === 'pricing-nav-link' || id === 'admin-plans-nav-link'
            ? new RegExp(`${id.split('-nav-link')[0].replace(/-(\w)/g, (_, c) => c.toUpperCase())}NavLink\\.style\\.display = 'none'`)
            : re;
        assert.ok(re.test(upd) || re2.test(upd), `${id} must stay hidden unconditionally in updateUIVisibility`);
    }
    for (const id of NEVER_REVEALED) {
        // FIX-CE-DASH-SWEEP-C2: the CE scrub DELETES some of these paid nav rows from
        // dashboard-shell.html (open-core/manifest.yaml rows 4390/4392 etc.), so the
        // "ships hidden" assert has no row to read on a CE cut - the stronger guarantee
        // (the row does not ship at all) holds there. Same tree-shape style as the
        // navRowsFloor guard: on the paid tree the assert stays VERBATIM; on the scrubbed
        // extract it prints a SKIP. The reveal-site half stays live on BOTH trees.
        if (shell.includes(`id="${id}"`)) {
            assert.ok(new RegExp(`id="${id}"[^>]*style="display:\\s*none`).test(shell), `${id} must ship hidden`);
        } else {
            console.log(`SKIP ${id} ships hidden: nav row scrubbed out of dashboard-shell.html (CE cut)`);
        }
        for (const f of jsFiles()) {
            assert.ok(!read('js', f).includes(id), `${f} references ${id}: it would be able to reveal a paid row`);
        }
    }

    // E4: route gates
    const setOf = (name) => {
        const at = uiSrc.indexOf(`const ${name} = new Set([`);
        assert.ok(at >= 0, `${name} not found`);
        return new Set([...uiSrc.slice(at, uiSrc.indexOf(']);', at)).matchAll(/'([a-z0-9-]+)'/g)].map((m) => m[1]));
    };
    const GATED = new Set();
    for (const n of ['_CE_COMPLIANCE_PAGES', '_CE_PAID_COMPLIANCE_PAGES', '_CE_COV4B_PAGES',
        '_CE_RETIRED_ADMIN_PAGES', '_CE_PAID_HUB_PAGES']) setOf(n).forEach((id) => GATED.add(id));
    const PAID_PAGES = ['custom-plans', 'promo-codes', 'free-trials', 'geographic-pricing',
        'seasonal-pricing', 'pricing-config', 'stack-billing', 'payments-admin', 'payment-settings',
        'coupons', 'pricing', 'business', 'admin-sso', 'admin-sso-domains', 'tenant-idp', 'admin-scim',
        'admin-scim-mapping', 'admin-scim-activity', 'gpu-pool', 'overage-caps', 'access-reviews',
        'capacity-report', 'adversary-findings', 'failure-prediction-calibration', 'mpc-sessions',
        'admin-dsar-requests', 'app-download', 'capture-devices', 'billing', 'admin-plans',
        'compliance-center', 'security', 'compliance', 'release-approvals', 'licence-review',
        'key-inventory', 'lockbox-approval', 'pending-approvals', 'consent-register'];
    // FIX-CE-DASH-SWEEP-C2: the CE scrub DELETES the denied page's entries from the
    // ui.js `_CE_*` Sets (open-core/manifest.yaml rows 4527/4586 etc.) together with the
    // page file itself (deny rows 2738/2799 etc.) - on a CE cut there is no panel left
    // for a #hash visit to render, so the gate assert has nothing to pin. Same tree-shape
    // style as the navRowsFloor guard: on the paid tree (every page file ships) the assert
    // stays VERBATIM for all 38 ids; on the extract it runs for the ids whose page still
    // ships and prints a SKIP for the carved ones.
    for (const id of PAID_PAGES) {
        if (exists('pages', `${id}.html`)) {
            assert.ok(GATED.has(id), `paid page '${id}' has no CE direct-URL gate: a #hash visit renders it on CE`);
        } else {
            console.log(`SKIP gate for '${id}': pages/${id}.html denied (carved out of this tree)`);
        }
    }
    for (const [name, ids] of [['_CE_COV4B_PAGES', ['custom-plans', 'promo-codes', 'free-trials',
        'geographic-pricing', 'seasonal-pricing']]]) {
        for (const id of ids) assert.ok(setOf(name).has(id), `${name} must carry '${id}'`);
    }
    assert.ok(/if \(_CE_COV4B_PAGES\.has\(targetPageId\) && await _isCeEdition\(\)\) \{/.test(uiSrc),
        'navigateToPage must gate _CE_COV4B_PAGES with _isCeEdition');
    for (const id of PAID_PAGES) {
        assert.ok(await isCe(CE_ALL), `the gate predicate must be true on CE for '${id}'`);
        assert.ok(!(await isCe(PRO_ALL)), `the gate predicate must be false on paid for '${id}' (paid unchanged)`);
    }

    // E5: hubs (the real _TAB_GATE/_tabAllowed source, evaluated for CE)
    function hubAllowed(file, ce, platformAdmin) {
        const src = read('js', file);
        const gs = src.indexOf('const _TAB_GATE');
        const ge = src.indexOf('function _visibleTabs');
        assert.ok(gs >= 0 && ge > gs, `${file}: gate region not found`);
        const ks = src.indexOf('const TAB_KEYS');
        const code = `let _isSuperAdmin = true;\nlet _isPlatformAdmin = ${platformAdmin};\nlet _ceEdition = ${ce};\n`
            + src.slice(ks, ge).replace(/^let _(isSuperAdmin|isPlatformAdmin|ceEdition)\b.*$/gm, '')
            + '\nJSON.stringify(TAB_KEYS.filter((k) => _tabAllowed(k)));';
        return JSON.parse(vm.runInNewContext(code, {}, { timeout: 5000 }));
    }
    for (const pa of [true, false]) {
        assert.deepStrictEqual(hubAllowed('security-hub.js', true, pa), ['active-sessions'],
            `Security hub on CE (platformAdmin=${pa}) must expose only Active Sessions`);
        assert.deepStrictEqual(hubAllowed('hub-compliance.js', true, pa), ['audit-logs'],
            `Compliance hub on CE (platformAdmin=${pa}) must expose only the Audit log`);
    }
    const sec = hubAllowed('security-hub.js', false, true);
    for (const k of ['alert-rules', 'ip-blocks', 'platform-access-review', 'security-alerts',
        'elevation-requests', 'abuse-queue', 'security-incidents', 'pending-approvals']) {
        assert.ok(sec.includes(k), `paid Security hub must still allow ${k} (no over-carve)`);
    }
    assert.ok(hubAllowed('hub-compliance.js', false, true).length > 5,
        'paid Compliance hub must keep its tabs');

    // E6: who reads the capability list
    const importers = jsFiles().filter((f) => /from '\.\/entitlements\.js'/.test(read('js', f))).sort();
    assert.deepStrictEqual(importers.filter((f) => f !== 'entitlements.js'),
        ['admin-scim.js', 'admin-sso.js', 'billing.js', 'gpu-pool.js', 'seller.js'].filter((f) => exists('js', f)).sort(),
        'a new MODEntitlements importer appeared: check its page against the CE API, then list it here');
    const readers = jsFiles().filter((f) => f !== 'entitlements.js'
        && /entitlements\??\.(capabilities|limits|tier_name)|\.capabilities\.includes|\.entitlements\.capabilities/.test(read('js', f)));
    assert.deepStrictEqual(readers, [], `capability/limit/tier readers outside entitlements.js: ${readers}`);
    const PAGE_OF = { 'admin-scim.js': 'admin-scim', 'admin-sso.js': 'admin-sso', 'gpu-pool.js': 'gpu-pool' };
    for (const [f, id] of Object.entries(PAGE_OF)) {
        if (!exists('js', f)) continue;
        assert.ok(/MODEntitlements\.lockElement\(/.test(read('js', f)), `${f} should still call lockElement`);
        assert.ok(GATED.has(id), `${f} lockElement runs on '${id}', which must be CE-gated`);
    }
    if (exists('js', 'seller.js')) {
        const seller = read('js', 'seller.js').replace(/^\s*\/\/.*$/gm, '');
        assert.ok(/if \(await isPaidEdition\(\)\) \{\s*MODEntitlements\.lockElement\(btn, 'marketplace_publish'/.test(seller),
            'seller.js: the stack lock stays inside the paid branch');
        assert.ok(/getElementById\('seller-kind-workflows'\)\?\.classList\.add\('hidden'\)/.test(seller),
            'seller.js: the CE branch keeps the workflow-publish block (the other lockElement) hidden');
    }

    // lockElement with every capability granted must leave the control untouched
    const entSrc = read('js', 'entitlements.js')
        .replace(/^import .*$/m, '')
        .replace(/^export default .*$/m, '');
    async function lockOutcome(features) {
        const win = {};
        const created = [];
        const doc = { createElement: (t) => { const e = { tag: t, children: [], appendChild(c) { this.children.push(c); },
            addEventListener() {}, set className(v) { this._c = v; } }; created.push(e); return e; } };
        new Function('window', 'document', 'getPlatformFeatures', entSrc)(win, doc, () => Promise.resolve(features));
        const el = { dataset: {}, classList: { add() {} }, title: '', disabled: false,
            insertAdjacentElement() { created.push('badge'); } };
        for (const cap of ['scim', 'sso', 'gpu_pool', 'marketplace_publish']) {
            await win.MODEntitlements.lockElement(el, cap, cap);
        }
        return { disabled: el.disabled, title: el.title, locked: el.dataset.modLocked || null };
    }
    const granted = await lockOutcome(CE_ALL);
    assert.deepStrictEqual(granted, { disabled: false, title: '', locked: null },
        'lockElement must not disable or badge anything when every capability is granted');
    const none = await lockOutcome(CE_NONE);
    assert.strictEqual(none.disabled, true, 'sanity: the same helper does lock with no capabilities');

    console.log(`ok  ce-entstub-all-granted-hides-paid: ${navRows.length} nav rows, ${PAID_PAGES.length} paid pages gated, hubs CE-minimal, 0 failures`);
})().catch((e) => { console.error(e); process.exit(1); });
