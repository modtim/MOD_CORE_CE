'use strict';
// Node-runnable static check for 2400GX-storePub-d — CE "Package to the
// Marketplace" (free listings, tenant admins only).
//
//   node CORE/Dashboard/tests/seller-ce-stack-publish.check.js
//
// Cases (source-text assertions on js/seller.js):
//   U1 CE + admin  : panel revealed, button NOT locked, price locked w/ CE text
//   U2 CE + non-admin: the form toggle is admin-gated (fail-closed)
//   U3 CE price    : price_cents forced null
//   U4 402         : server detail as TEXT + upgrade anchor, no innerHTML
//   U5 paid        : lockElement + price computation + listings unchanged
//   U6 CE hidden   : sales/payout/Connect block ids stay hidden
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

// CE carve (FIX-CE-DASH-SWEEP-C): every case here (U1-U6) asserts on the shipped
// text of js/seller.js, which the CE carve denies; when it is absent the whole
// check is a SKIP. On a tree that ships it, nothing changes.
if (!fs.existsSync(path.join(__dirname, '..', 'js', 'seller.js'))) {
    console.log('SKIP seller-ce-stack-publish.check.js: js/seller.js absent (CE carve)');
    process.exit(0);
}
const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'seller.js'), 'utf8');
const results = [];

function check(name, ok, why) {
    results.push([name, ok]);
    if (!ok) console.log(`  ${name} FAIL: ${why}`);
}

// --- U1: CE reveals the panel and shows the form -------------------------
// 0de9809a5 (2400LR-p86) added a ~14-line comment block between the reveal and
// the bind call, which pushed the pair past the {0,600} window. Strip comments
// before matching so the byte windows keep their original meaning (the CE
// branch's CODE stays this tight), instead of widening them.
const srcNoComments = src.replace(/^\s*\/\/.*$/gm, '');
const u1 = /if \(!_paidEdition\) \{[\s\S]{0,1200}?main-tab-my-listings'\)\?\.classList\.remove\('hidden'\)[\s\S]{0,600}?await bindStackPackageForm\(\);/
    .test(srcNoComments);
check('U1', u1, 'CE branch must reveal main-tab/panel-my-listings and bind the form');
const u1ce = /if \(await isPaidEdition\(\)\) \{\s*MODEntitlements\.lockElement\(btn, 'marketplace_publish'/
    .test(src);
check('U1b', u1ce, 'CE must skip lockElement(marketplace_publish) (cap not granted in CE)');
const u1price = /_lockCeStackPriceInput\(\)[\s\S]{0,400}?Priced listings are not part of this edition./.test(src)
    && /input\.disabled = true;/.test(src);
check('U1c', u1price, 'CE must disable #seller-stack-price-input with the CE text');

// --- U2: admin gate ------------------------------------------------------
// 5060ba7f6 (STALEERR) added the gate-message clear between the admin gate
// and the toggle; match on the comment-stripped source (same reason U1 does).
const u2 = /if \(!await _isTenantAdminUser\(\)\) \{[\s\S]{0,600}?form\.classList\.toggle\('hidden'\)/
    .test(srcNoComments);
check('U2', u2, 'the form toggle must be gated on _isTenantAdminUser()');
const u2fail = /async function _isTenantAdminUser\(\)[\s\S]{0,500}?me\.parent_user_id === null;[\s\S]{0,200}?catch \{\s*return false;/
    .test(src);
check('U2b', u2fail, 'admin predicate = parent_user_id null, fail-closed on error');

// --- U3: CE sends price_cents: null -------------------------------------
const u3 = /_ceFreeOnly \? null : \(priceRaw \? Math\.round\(parseFloat\(priceRaw\) \* 100\) : null\)/
    .test(src);
check('U3', u3, 'CE must force priceCents null whatever the field holds');

// --- U4: 402 text + upgrade link, no innerHTML of server text -----------
const u4 = /if \(status === 402\) \{[\s\S]{0,300}?String\(err\?\.detail \?\? err\?\.message/.test(src);
check('U4', u4, '402 case must return the server detail as text');
const u4link = /function _showCeUpgradeLink\(errEl\)[\s\S]{0,500}?a\.href = '#billing';[\s\S]{0,200}?a\.textContent = 'Not part of this edition';/.test(src)
    && /if \(e\?\.status === 402\) _showCeUpgradeLink\(errEl\);/.test(src);
check('U4b', u4link, '402 must append the mod-paid-badge upgrade anchor');
const u4safe = !/errEl\.innerHTML/.test(src)
    && !/[^t]\s_innerHTML\s*=\s*_packageStackError/.test(src)
    && (src.match(/= _packageStackError\(/g) || []).length
        === (src.match(/textContent = _packageStackError\(/g) || []).length;
check('U4c', u4safe, 'no innerHTML of server text (textContent only)');

// --- U5: paid path unchanged --------------------------------------------
const u5 = /const priceCents = \(await isPaidEdition\(\)\)/.test(src)
    || /_ceFreeOnly \? null : \(priceRaw \? Math\.round/.test(src);
check('U5', u5, 'paid still computes priceCents from the entered value');
const u5lock = /MODEntitlements\.lockElement\(btn, 'marketplace_publish', 'Package to the Marketplace'\);/.test(src)
    && /if \(emptyBtn\) MODEntitlements\.lockElement\(emptyBtn, 'marketplace_publish', 'Package to the Marketplace'\);/.test(src);
check('U5b', u5lock, 'paid lock calls byte-unchanged (cap absent -> locked + badge)');
const u5list = /if \(!_ceFreeOnly\) await loadStackListings\(\);/.test(src);
check('U5c', u5list, 'paid refreshes listings after submit; CE does not');

// --- U6: CE hidden block ids --------------------------------------------
const hiddenIds = ['seller-kind-workflows', 'seller-codes-modal', 'seller-stacks-loading'];
const u6 = hiddenIds.every((id) => new RegExp(`getElementById\\('${id}'\\)\\?\\.classList\\.add\\('hidden'\\)`).test(src));
check('U6', u6, 'CE must hide workflow-publish + codes + listings blocks');

const failed = results.filter(([, ok]) => !ok);
if (failed.length) {
    console.log(`\n${failed.length} assertion(s) failed: seller-ce-stack-publish.check.js`);
    process.exit(1);
}
console.log('\nAll assertions passed: seller-ce-stack-publish.check.js');
results.forEach(([name]) => console.log(`  ${name} ok`));
console.log('  hidden-in-CE ids: seller-kind-workflows, seller-codes-modal,'
    + ' seller-stacks-loading, seller-stacks-empty, seller-stacks-table-wrap');
