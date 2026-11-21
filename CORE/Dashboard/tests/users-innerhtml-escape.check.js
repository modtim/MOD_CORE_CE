'use strict';
// 2400LR-relinkXSS2 — the six innerHTML sinks in users.js that relinkXSS
// (7d6c2ddf2) left behind still interpolate SERVER/USER values raw:
//   invitations row (~:1071)  -> invitation.email
//   tenant row + deleteBtn (~:1453/:1457) -> tenant.name (text + data-name)
//   custom-plan row (~:1712) -> plan.name, tenantName, data-plan-name
//   capability section/category/label (~:2219/:2240/:2257) -> displayName,
//       categoryName (text + data-category/data-category-container),
//       cap.display_name, cap.description
//   roles row + badges (~:2349/:2345) -> role.name, role.description,
//       c.display_name || c.name
// Fix = wrap each in the escaper already in scope (_escapeHtml, users.js:646).
// Constants/numbers (ids, prices, counts, status badges) stay as they are.
//
//   node --test CORE/Dashboard/tests/users-innerhtml-escape.check.js
//
// Cases:
//   E0 the only escaper in users.js is the pre-existing _escapeHtml
//   E1 the escaper neutralises the payload
//   E2 each (S) sink wraps its server/user value with _escapeHtml
//   E3 rendering the shipped template with the payload yields no live tag/quote
//   E4 the old bare interpolations are gone (HEAD users.js fails here)
//   E5 no double escaping, textContent helpers still get raw text
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const usersSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'users.js'), 'utf8');
const results = [];
function check(name, ok, why) {
    results.push([name, ok]);
    if (!ok) console.log(`  ${name} FAIL: ${why}`);
}

// --- template scanner: pull a shipped template literal verbatim from users.js
function findTemplate(src, from) {
    const start = src.indexOf('`', from);
    if (start < 0) return null;
    let i = start + 1;
    while (i < src.length) {
        const ch = src[i];
        if (ch === '\\') { i += 2; continue; }
        if (ch === '`') return { text: src.slice(start + 1, i), end: i + 1 };
        if (ch === '$' && src[i + 1] === '{') { i = skipExpr(src, i + 1); continue; }
        i++;
    }
    return null;
}
function skipExpr(src, i) { // i at '{'
    let depth = 0;
    while (i < src.length) {
        const ch = src[i];
        if (ch === '\\') { i += 2; continue; }
        if (ch === '`') {
            const t = findTemplate(src, i);
            i = t ? t.end : src.length;
            continue;
        }
        if (ch === '\'' || ch === '"') {
            let j = i + 1;
            while (j < src.length) { if (src[j] === '\\') { j += 2; continue; } if (src[j] === ch) break; j++; }
            i = j + 1;
            continue;
        }
        if (ch === '{') { depth++; i++; continue; }
        if (ch === '}') { depth--; i++; if (depth === 0) return i; continue; }
        i++;
    }
    return src.length;
}
function extractTemplate(src, anchor, skip) {
    let pos = src.indexOf(anchor);
    if (pos < 0) return null;
    for (let n = 0; n < skip; n++) {
        const t = findTemplate(src, pos);
        if (!t) return null;
        pos = t.end;
    }
    const t = findTemplate(src, pos);
    return t ? t.text : null;
}
function extractFn(src, marker) {
    const start = src.indexOf(marker);
    if (start < 0) return null;
    let depth = 0;
    for (let i = src.indexOf('{', start); i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (depth === 0) return src.slice(start, i + 1); }
    }
    return null;
}

// --- E0: one escaper only, the pre-existing one ----------------------------
const decls = (usersSrc.match(/^function _escapeHtml\(/gm) || []).length;
check('E0a', decls === 1, `users.js keeps exactly one _escapeHtml decl (got ${decls})`);
check('E0b', !/function (escapeHtml|_esc|htmlEscape)\(/.test(usersSrc), 'no second escaper was added');
const escSrc = extractFn(usersSrc, 'function _escapeHtml(s)');
check('E0c', !!escSrc, '_escapeHtml must exist in users.js');

const PAYLOAD = '<img src=x onerror=1>"\'&';
let esc = null;
if (escSrc) {
    esc = eval('(' + escSrc + ')');
    check('E1a', esc(PAYLOAD).includes('&lt;img'), 'escaper turns <img into &lt;img');
    check('E1b', !/<img/.test(esc(PAYLOAD)), 'no live <img survives');
    check('E1c', esc(PAYLOAD).includes('&quot;') && esc(PAYLOAD).includes('&#39;') && esc(PAYLOAD).includes('&amp;'), 'quotes and & are escaped');
    check('E1d', esc('plain') === 'plain', 'plain text untouched');
}

// --- the (S) sinks: shipped template + the server/user values it interpolates
const SINKS = [
    { n: 'A_invitation_email', anchor: 'invs.forEach(invitation => {', skip: 0,
      vars: ['invitation'], scope: { invitation: { email: PAYLOAD, expires_at: '2026-01-01T00:00:00Z', id: 'inv-1' } } },
    { n: 'B_tenant_deleteBtn', anchor: 'const deleteBtn = tenant.id === 1', skip: 0,
      vars: ['tenant'], scope: { tenant: { id: 7, name: PAYLOAD } } },
    { n: 'C_tenant_row', anchor: 'const deleteBtn = tenant.id === 1', skip: 1,
      vars: ['tenant', 'storageDisplay', 'overrideCell', 'createdDate', 'deleteBtn'],
      scope: { tenant: { id: 7, name: PAYLOAD, credit_balance: 100 }, storageDisplay: '1 GB', overrideCell: '', createdDate: '2026-01-01', deleteBtn: '' } },
    { n: 'D_plan_row', anchor: 'const isAssigned = tenant && tenant.current_plan_id === plan.id;', skip: 1,
      vars: ['plan', 'tenantName', 'price', 'limits', 'statusBadge'],
      scope: { plan: { name: PAYLOAD, id: 3, custom_for_tenant_id: 7 }, tenantName: PAYLOAD, price: 'Free', limits: '1 seats', statusBadge: '' } },
    { n: 'E_capability_section', anchor: 'section.innerHTML = `', skip: 0,
      vars: ['resourceType', 'displayName', 'selectedCount', 'totalCount'],
      scope: { resourceType: 'node', displayName: PAYLOAD, selectedCount: 1, totalCount: 2 } },
    { n: 'F_category_div', anchor: 'categoryDiv.innerHTML = `', skip: 0,
      vars: ['categoryName', 'resourceType'], scope: { categoryName: PAYLOAD, resourceType: 'node' } },
    { n: 'G_capability_label', anchor: 'label.innerHTML = `', skip: 0,
      vars: ['cap', 'isChecked'],
      scope: { cap: { display_name: PAYLOAD, id: 'cap-1', description: PAYLOAD, is_sensitive: true }, isChecked: true } },
    { n: 'H_role_badge', anchor: 'capabilityBadges = capabilities', skip: 0,
      vars: ['c'], scope: { c: { display_name: PAYLOAD, name: PAYLOAD } } },
    { n: 'I_role_row', anchor: 'const capabilities = role.capabilities || [];', skip: 1,
      vars: ['role', 'capabilityBadges'],
      scope: { role: { name: PAYLOAD, description: PAYLOAD, id: 5 }, capabilityBadges: '' } },
];

const WRAPPED = [
    '_escapeHtml(invitation.email)', '_escapeHtml(tenant.name)', '_escapeHtml(plan.name)',
    '_escapeHtml(tenantName)', '_escapeHtml(plan.name || \'\')', '_escapeHtml(displayName)',
    '_escapeHtml(categoryName)', '_escapeHtml(cap.display_name)', '_escapeHtml(cap.description)',
    '_escapeHtml(c.display_name || c.name)', '_escapeHtml(role.name)', '_escapeHtml(role.description || \'-\')',
];
const BARE = [
    />\$\{invitation\.email\}</, /data-name="\$\{tenant\.name\./, />\$\{tenant\.name\}</,
    />\$\{plan\.name\}</, />\$\{tenantName\}</, /data-plan-name="\$\{\(plan\.name/,
    />\$\{displayName\}</, />\$\{categoryName\}</, /data-category="\$\{resourceType\}-\$\{categoryName\}"/,
    /data-category-container="\$\{resourceType\}-\$\{categoryName\}"/, />\$\{cap\.display_name\}</,
    />\$\{cap\.description\}</, />\$\{c\.display_name \|\| c\.name\}</, />\$\{role\.name\}</, />\$\{role\.description \|\| '-'\}</,
];

if (!esc) {
    SINKS.forEach(s => check(s.n, false, 'escaper unavailable'));
} else {
    const escapedOut = esc(PAYLOAD);
    for (const s of SINKS) {
        const tmpl = extractTemplate(usersSrc, s.anchor, s.skip);
        if (!tmpl) { check(s.n, false, `template for ${s.n} not found in users.js`); continue; }
        let out = null;
        try { out = new Function(...s.vars, '_escapeHtml', 'return `' + tmpl + '`')(...s.vars.map(v => s.scope[v]), esc); }
        catch (e) { check(s.n, false, `template for ${s.n} did not render: ${e}`); continue; }
        const ok = out.includes(escapedOut) && !/<img/.test(out) && !out.includes(PAYLOAD);
        check(s.n, ok, `${s.n} must render ${JSON.stringify(escapedOut)} with no live tag/quote (raw payload ${out.includes(PAYLOAD)})`);
    }
    for (const w of WRAPPED) check('W_' + w, usersSrc.includes(w), `users.js must wrap ${w}`);
    for (let i = 0; i < BARE.length; i++) check('X_' + i, !BARE[i].test(usersSrc), `bare interpolation #${i} must be gone`);
}

// --- E5: no double escaping, textContent helpers keep raw text -------------
check('E5a', !/_escapeHtml\(\s*_escapeHtml\(/.test(usersSrc), 'no _escapeHtml(_escapeHtml(...)) double escape');
check('E5b', !/showInfo\(_escapeHtml\(/.test(usersSrc), 'showInfo (textContent) must get raw text');
check('E5c', !/showError\(_escapeHtml\(/.test(usersSrc), 'showError (textContent) must get raw text');

const failed = results.filter(r => !r[1]);
if (failed.length) {
    console.log(`users-innerhtml-escape.check.js: ${failed.length}/${results.length} FAILED`);
    process.exit(1);
}
console.log(`users-innerhtml-escape.check.js: all ${results.length} checks passed -> OK`);
