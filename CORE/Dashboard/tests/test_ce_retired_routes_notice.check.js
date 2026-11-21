'use strict';
// FIX-CE-NAV-REGROUP-CLASS-B — proof that every route in the e2e sweep's
// CE_SWEEP_RETIRED set gets the amber "Not available in MOD Community Edition"
// panel. No browser: the ids are parsed out of the spec text, the registry is
// loaded the way test_page_registry.check.js loads it, and the gate functions
// are taken VERBATIM out of CORE/Dashboard/js/ui.js (the CE extract's own code)
// and run against the CE view of the registry (paid rows scrubbed out, exactly
// as open-core/manifest.yaml does).
// A retired route whose id is NOT denied, or is denied but owned by no panel
// branch, is the route whose notice never renders.

const fs = require('fs');
const path = require('path');
const assert = require('assert');
const test = require('node:test');

const JS = path.join(__dirname, '..', 'js');
// The nodetest harness mounts /app = CORE/, so from /app/tests the spec is one
// level up; in the repo (CORE/Dashboard/tests) it is two. Try both.
function specPath() {
    const cands = [path.join(__dirname, '..', '..', 'e2e', 'tests', 'dash-admin-nav-regroup.spec.ts'),
        path.join(__dirname, '..', 'e2e', 'tests', 'dash-admin-nav-regroup.spec.ts')];
    for (const c of cands) if (fs.existsSync(c)) return c;
    return null; // nodetest harness: /app = CORE/Dashboard, the spec is not mounted
}
const registrySrc = fs.readFileSync(path.join(JS, 'page-registry.js'), 'utf8');
const manifestSrc = fs.readFileSync(path.join(JS, 'ui-manifest.js'), 'utf8');
const uiSrc = fs.readFileSync(path.join(JS, 'ui.js'), 'utf8');

function loadEsm(src, deps) {
    const declared = [...src.matchAll(/^export (?:const|function) (\w+)/gm)].map((m) => m[1]);
    let body = src.replace(/^import \{ ([^}]*) \} from '([^']+)';$/gm,
        (m, names, spec) => `const {${names}} = __deps[${JSON.stringify(spec)}];`);
    body = body.replace(/^export \{ ([^}]*) \};$/gm, (m, names) => `var __reexp = {${names}};`);
    body = body.replace(/^export (const|function) /gm, '$1 ');
    body += `\nreturn {${declared.join(', ')}, __reexp: (typeof __reexp === 'undefined' ? {} : __reexp)};`;
    const out = new Function('__deps', body)(deps);
    return { exp: out, reexp: out.__reexp };
}

function stripComments(src) {
    return src.replace(/^[ \t]*\/\/.*$/gm, '');
}

function sliceBalanced(src, openIdx) {
    let depth = 0;
    for (let i = openIdx; i < src.length; i += 1) {
        if (src[i] === '[' || src[i] === '{') depth += 1;
        else if (src[i] === ']' || src[i] === '}') { depth -= 1; if (depth === 0) return src.slice(openIdx, i + 1); }
    }
    throw new Error('unbalanced at ' + openIdx);
}

function grabSetText(name) {
    const at = uiSrc.indexOf('const ' + name + ' = new Set([');
    if (at < 0) throw new Error('set not found: ' + name);
    return sliceBalanced(uiSrc, uiSrc.indexOf('[', at));
}

function grabFn(name) {
    const at = uiSrc.indexOf('function ' + name + '(');
    if (at < 0) throw new Error('fn not found: ' + name);
    const headerEnd = uiSrc.indexOf('{', at);
    return uiSrc.slice(at, headerEnd) + sliceBalanced(uiSrc, headerEnd);
}

const UI = stripComments(uiSrc);
const SET_NAMES = ['_CE_COMPLIANCE_PAGES', '_CE_PAID_COMPLIANCE_PAGES', '_CE_COV4B_PAGES',
    '_CE_PAID_HUB_PAGES', '_CE_RETIRED_ADMIN_PAGES'];
const FN_NAMES = ['getPageEntry', '_ceDeniedPageId', '_ceRetiredAdminGate'];

function freshRegistry() {
    const pr = loadEsm(registrySrc, {}).exp;
    pr.PAGE_REGISTRY.length = 0;
    return pr;
}

function ceRows() {
    // Paid tree registers all 106 rows + aliases; the CE extract scrubs every
    // `paid: true` row out, so the CE gate sees only the free rows.
    const pr = freshRegistry();
    const mf = loadEsm(manifestSrc, { './page-registry.js': pr }).exp;
    mf.registerDashboardPages();
    mf.registerCeGateAliases();
    // CE-NOPAID-4.1d: paid rows + the model-cache alias load via the seam;
    // in this lifted context run the paid file's own fns against the same registry shim.
    const paidSrc = fs.readFileSync(path.join(JS, '..', '..', 'API', 'paid_addon', 'dashboard', 'ui-manifest-paid.js'), 'utf8');
    const paidExp = loadEsm(paidSrc, {}).exp;
    paidExp.registerPaidRows(pr.registerPage);
    paidExp.registerCeGateAliases((e) => { pr.PAGE_REGISTRY.push(e); return e; });
    const paid = pr.PAGE_REGISTRY.filter((e) => e.paid === true).length;
    const kept = pr.PAGE_REGISTRY.filter((e) => e.paid !== true);
    pr.PAGE_REGISTRY.length = 0;
    kept.forEach((e) => pr.PAGE_REGISTRY.push(e));
    return { rows: mf._ceRegisteredRows(), paid, total: paid + kept.length };
}

function uiGate(rows) {
    const body = SET_NAMES.map((n) => `const ${n} = new Set(${grabSetText(n)});`).join('\n')
        + '\n' + FN_NAMES.map((n) => grabFn(n)).join('\n')
        + '\nreturn {' + FN_NAMES.join(',') + ',' + SET_NAMES.join(',') + '};';
    return new Function('_ceRegisteredRows', body)(rows);
}

// Parsed (not hand-copied) out of the spec's CE_SWEEP_RETIRED set on 11-14 with:
//   node -e "...stripComments(spec)...sliceBalanced...matchAll(/'([a-z0-9-]+)'/g)..."
// The nodetest harness mounts /app = CORE/Dashboard only (plus /API + /open-core), so
// CORE/e2e/tests/dash-admin-nav-regroup.spec.ts is NOT visible there: when the spec IS
// readable (repo / any full-tree run) the parsed ids must equal this list, so drift in
// the spec fails loudly; when it is not, this parsed list is the input.
const SPEC_IDS_PARSED = ['admin-dsar-requests', 'pricing-config', 'admin-scim-activity',
    'key-inventory', 'compliance', 'auditor-access', 'parental-consent', 'security-lockdown',
    'outbound-connections', 'release-approvals', 'licence-review', 'lockbox-approval',
    'consent-register', 'admin-scim-mapping', 'admin-sso', 'admin-sso-domains', 'tenant-idp',
    'business', 'payments-admin', 'capture-devices', 'stack-billing', 'app-download',
    'coupons', 'security', 'compliance-center', 'billing', 'admin-plans', 'custom-plans',
    'free-trials', 'promo-codes', 'geographic-pricing', 'seasonal-pricing', 'subscription'];

function retiredIds() {
    const sp = specPath();
    if (!sp) return SPEC_IDS_PARSED;
    const src = stripComments(fs.readFileSync(sp, 'utf8'));
    const at = src.indexOf('const CE_SWEEP_RETIRED = new Set([');
    assert.ok(at > -1, 'CE_SWEEP_RETIRED not found in spec');
    const arr = sliceBalanced(src, src.indexOf('[', at));
    const parsed = [...new Set([...arr.matchAll(/'([a-z0-9][a-z0-9-]*)'/g)].map((m) => m[1]))];
    assert.deepStrictEqual(parsed, SPEC_IDS_PARSED, 'CE_SWEEP_RETIRED drifted from SPEC_IDS_PARSED');
    return parsed;
}

test('CE_SWEEP_RETIRED: every route is denied AND a panel branch owns it', () => {
    const reg = ceRows();
    const gate = uiGate(() => reg.rows);
    const ids = retiredIds();
    assert.ok(ids.length >= 23, 'parsed too few ids: ' + ids.length);
    const failures = [];
    for (const id of ids) {
        const denied = gate._ceDeniedPageId(id);
        const retiredAdmin = gate._ceRetiredAdminGate(id);
        const panel = denied && (retiredAdmin
            || gate._CE_COV4B_PAGES.has(id)
            || gate._CE_COMPLIANCE_PAGES.has(id)
            || gate._CE_PAID_COMPLIANCE_PAGES.has(id)
            || (gate._CE_PAID_HUB_PAGES.has(id) && id !== 'marketplace'));
        if (!panel) failures.push(id + (denied ? ':denied-no-panel' : ':NOT-denied'));
    }
    console.log('retired ids=' + ids.length + ' registry rows=' + reg.total + ' paid=' + reg.paid);
    console.log('FAILING=' + (failures.length ? failures.join(' ') : 'none'));
    assert.deepStrictEqual(failures, []);
});

test('every panel helper the gate can pick ships the pinned h1', () => {
    const H1 = 'Not available in MOD Community Edition';
    for (const fn of ['_ceCompliancePanelHtml', '_cePaidPagePanelHtml',
        '_cePaidCompliancePanelHtml', '_ceRetiredAdminPanelHtml']) {
        const body = grabFn(fn);
        assert.ok(body.includes('<h1') && body.includes(H1), fn + ': no pinned h1');
    }
    // navigateToPage actually calls each helper into #page-content.
    const nav = uiSrc.slice(uiSrc.indexOf('export async function navigateToPage'));
    for (const fn of ['_ceCompliancePanelHtml()', '_cePaidPagePanelHtml()',
        '_cePaidCompliancePanelHtml()', '_ceRetiredAdminPanelHtml(targetPageId)']) {
        assert.ok(nav.includes("innerHTML = " + fn), 'navigateToPage never paints ' + fn);
    }
});
