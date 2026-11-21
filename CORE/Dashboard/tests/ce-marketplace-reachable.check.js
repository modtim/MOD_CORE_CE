'use strict';
// FIX-CE-DASH-TESTS-CE-MARKETPLACE-REACHABLE (11-16) - static check for the CE store/
// seller posture AFTER the owner 11-16 store-deny decision (CE has NO store scope). Follow-up
// of  (a2043cbbc6) and
//  (E51DB1E65E).
//
//   node CORE/Dashboard/tests/ce-marketplace-reachable.check.js
//
// js/ui.js: the CE hub gate (_CE_PAID_HUB_PAGES + _isCeEdition) carries NO page-level
// marketplace exception any more, and the store set (marketplace, library, seller,
// listing-review) is denied, so CE shows the Not-available panel for all four. The CE
// sidebar hides those nav entries via _CE_STORE_NAV_PAGES + _hideCeStoreNav (fail-closed:
// unknown edition = CE). js/seller.js is DENIED on CE, so the cases that read it (C5-C11)
// SKIP there and stay live on the paid tree. Cases (SOURCE TEXT only, never a payload):
//   C1 hub gate: no marketplace exemption + set carries the four store ids
//   C2 _CE_PAID_HUB_PAGES unchanged (six members)
//   C3 gate still fail-closed (panel + return)
//   C4 store nav entries hidden on CE (_hideCeStoreNav called)
//   C5 seller init reaches the CE branch and binds the form (paid-tree case)
//   C6 paid affordance blocks stay hidden
//   C7 paid marketplace_publish lock stays in the paid branch
//   C8 CE price input locked
//   C9 CE submit sends price_cents null
//   C10 admin gate fail-closed
//   C11 paid path unchanged
//   C12 no secret/host/URL literal in this file
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const uiSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'ui.js'), 'utf8');
// CE carve (FIX-CE-DASH-SWEEP-C): js/seller.js is denied on CE. C5-C11 read it,
// so they SKIP when it is absent; the ui.js cases (C1-C4) and C12 stay live.
const hasSeller = fs.existsSync(path.join(__dirname, '..', 'js', 'seller.js'));
const sellerSrc = hasSeller ? fs.readFileSync(path.join(__dirname, '..', 'js', 'seller.js'), 'utf8') : '';
const selfSrc = fs.readFileSync(path.join(__dirname, 'ce-marketplace-reachable.check.js'), 'utf8');
const snc = sellerSrc.replace(/^\s*\/\/.*$/gm, '');
const results = [];

function check(name, ok, why) {
    results.push([name, ok]);
    if (!ok) console.log(`  ${name} FAIL: ${why}`);
}

// --- C1: owner 11-16 store-deny decision - the marketplace exemption is GONE and the
// set carries the store pages, so CE shows the "Not available" panel for all of them -----
const gateRe = /if \(_CE_PAID_HUB_PAGES\.has\(targetPageId\) && await _isCeEdition\(\)\) \{\s*document\.getElementById\('page-content'\)\.innerHTML = _cePaidPagePanelHtml\(\);/;
const setStart = uiSrc.indexOf('const _CE_PAID_HUB_PAGES = new Set([');
const setLitText = uiSrc.slice(setStart, uiSrc.indexOf(']);', setStart));
const storeSetOk = ['marketplace', 'library', 'seller', 'listing-review'].every((id) => setLitText.includes("'" + id + "',"));
check('C1', gateRe.test(uiSrc) && !/targetPageId !== 'marketplace'/.test(uiSrc) && storeSetOk,
    'the CE hub gate must have NO marketplace exemption and the set must carry marketplace, library, seller, listing-review');

// --- C2: the set literal is unchanged (six members) -----------------------
const setAt = uiSrc.indexOf('const _CE_PAID_HUB_PAGES = new Set([');
const setLit = setAt < 0 ? '' : uiSrc.slice(setAt, uiSrc.indexOf('];', setAt) + 2);
const hubMembers = ['marketplace', 'security', 'compliance-center', 'billing', 'admin-plans', 'capture-devices'];
check('C2', setAt >= 0 && hubMembers.every((id) => new RegExp(`'${id}',?`).test(setLit)),
    `_CE_PAID_HUB_PAGES must still carry all six members (${hubMembers.join(', ')})`);

// --- C3: the gate keeps its fail-closed shape (neighbour window) ----------
const shapeRe = /if \(_CE_PAID_HUB_PAGES\.has\(targetPageId\) && await _isCeEdition\(\)\) \{[\s\S]{0,300}?_cePaidPagePanelHtml\(\);[\s\S]{0,200}?return;/;
check('C3', shapeRe.test(uiSrc),
    'the hub gate must still render _cePaidPagePanelHtml() and return (fail-closed for the other hub pages)');

// --- C4: FIX-CE-DASH-HIDE-STORE-NAV (owner 11-16) - the store NAV ENTRIES are HIDDEN on CE ---
// The sidebar offer is closed by its OWN fail-closed helper: _CE_STORE_NAV_PAGES carries the
// four store ids, _hideCeStoreNav hides `<id>-nav-link` under _isCeEdition() (unknown = CE),
// and updateUIVisibility CALLS it right after _applyCePaidHubNav. RED under mutation: delete
// the helper or its call and this case fails (the nav entry would be offered on CE again).
const storeSetAt = uiSrc.indexOf('const _CE_STORE_NAV_PAGES = new Set([');
const storeLit = storeSetAt < 0 ? '' : uiSrc.slice(storeSetAt, uiSrc.indexOf(']);', storeSetAt));
const storeNavIds = ['marketplace', 'library', 'seller', 'listing-review'];
const hideFnRe = /function _hideCeStoreNav\(\) \{[\s\S]{0,400}?_isCeEdition\(\)[\s\S]{0,400}?style\.display = 'none';/;
const callRe = /_hideCeStoreNav\(\);/;
check('C4', storeSetAt >= 0 && storeNavIds.every((id) => storeLit.includes("'" + id + "',"))
    && hideFnRe.test(uiSrc) && callRe.test(uiSrc)
    && /function _applyCePaidHubNav\(/.test(uiSrc) && /'marketplace-nav-link': true,/.test(uiSrc),
    'the four store nav ids must be in _CE_STORE_NAV_PAGES, _hideCeStoreNav must hide them under _isCeEdition (fail-closed) and be called, and _applyCePaidHubNav must keep the paid reveal');

if (!hasSeller) console.log('SKIP C5-C11: js/seller.js absent (CE carve)');
if (hasSeller) {
// --- C5: CE reveals BOTH tab and panel, then binds the form --------------
const headRe = /export async function initializeSellerPage\(\) \{[\s\S]{0,300}?if \(!_paidEdition\) \{/;
const guardRe = /export async function initializeSellerPage\(\) \{[\s\S]{0,200}?if \(!await isPaidEdition\(\)\) return;/;
const revealRe = /if \(!_paidEdition\) \{[\s\S]{0,1200}?main-tab-my-listings'\)\?\.classList\.remove\('hidden'\)[\s\S]{0,600}?main-panel-my-listings'\)\?\.classList\.remove\('hidden'\)[\s\S]{0,600}?await bindStackPackageForm\(\);/;
check('C5', headRe.test(snc) && !guardRe.test(snc) && revealRe.test(snc),
    'initializeSellerPage must reach the CE branch (no paid early-return) and reveal main-tab + main-panel then bind the form');

// --- C6: CE still hides the paid affordance blocks -----------------------
const hideIds = ['seller-kind-workflows', 'seller-codes-modal', 'seller-stacks-loading'];
check('C6', hideIds.every((id) => new RegExp(`getElementById\\('${id}'\\)\\?\\.classList\\.add\\('hidden'\\)`).test(snc)),
    'CE must keep hiding seller-kind-workflows / seller-codes-modal / seller-stacks-loading');

// --- C7: CE skips the paid lock -----------------------------------------
check('C7', /if \(await isPaidEdition\(\)\) \{\s*MODEntitlements\.lockElement\(btn, 'marketplace_publish'/.test(snc),
    'cap.MARKETPLACE_PUBLISH lock must stay inside the paid branch (CE skips it)');

// --- C8: CE price lock ---------------------------------------------------
const priceOk = /_lockCeStackPriceInput\(\)[\s\S]{0,400}?Priced listings are not part of this edition./.test(snc)
    && /\} else \{\s*_lockCeStackPriceInput\(\);/.test(snc)
    && /input\.disabled = true;/.test(snc);
check('C8', priceOk,
    'the CE else-branch must call _lockCeStackPriceInput() (input.disabled = true + the CE text)');

// --- C9: CE sends price_cents null --------------------------------------
check('C9', /_ceFreeOnly \? null : \(priceRaw \? Math\.round\(parseFloat\(priceRaw\) \* 100\) : null\)/.test(snc),
    'CE submit must force price_cents null (never a client-supplied price)');

// --- C10: admin gate fail-closed ----------------------------------------
const adminRe = /if \(!await _isTenantAdminUser\(\)\) \{[\s\S]{0,600}?form\.classList\.toggle\('hidden'\)/.test(snc);
const adminFn = /async function _isTenantAdminUser\(\)[\s\S]{0,400}?me\.parent_user_id === null[\s\S]{0,200}?catch \{[\s\S]{0,60}?return false;/.test(snc);
check('C10', adminRe && adminFn,
    'the stack-package form toggle must stay admin-gated and _isTenantAdminUser must fail closed (catch -> false)');

// --- C11: paid path unchanged -------------------------------------------
const fnMatch = /export async function initializeSellerPage\(\)[\s\S]*?\n\}/.exec(sellerSrc);
const fnBody = fnMatch ? fnMatch[0] : '';
check('C11', /bindKindTabs\(\);/.test(fnBody) && /bindWorkflowPublishForm\(\);/.test(fnBody)
    && /bindCodesModal\(\);/.test(fnBody) && !/if \(!await isPaidEdition\(\)\) return;/.test(fnBody),
    'the paid path (bindKindTabs + bindWorkflowPublishForm + bindCodesModal) must stay and carry no bare paid early-return');

}

// --- C12: no secret / host / URL literal in this file -------------------
const leakRe = new RegExp('https?://|Bearer\\s+[A-Za-z0-9+/.-_]{10,}|' + '-'.repeat(5) + 'BEGIN|[0-9]{1,3}.[0-9]{1,3}.[0-9]{1,3}.[0-9]{1,3}');
check('C12', !leakRe.test(selfSrc),
    'this check must carry no URL, host, IP or token literal (source-text assertions only)');

const failed = results.filter(([, ok]) => !ok);
assert.ok(true, 'ce-marketplace-reachable.check.js loaded');
console.log(`# pass ${results.length - failed.length} / # fail ${failed.length}`);
if (failed.length) {
    console.log(`ce-marketplace-reachable.check.js: ${failed.length} assertion(s) failed`);
    process.exit(1);
}
console.log('ce-marketplace-reachable.check.js: all 12 cases ok');
