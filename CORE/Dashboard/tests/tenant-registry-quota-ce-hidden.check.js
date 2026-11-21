// FIX-CE-HIDE-ADMIN-STORAGE-QUOTA: the Tenant Registry storage-quota bar is hidden on
// CE (no /harbor/tenant-quota call) and shown only on a positively-paid edition.
//   node --test CORE/Dashboard/tests/tenant-registry-quota-ce-hidden.check.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'tenant-registry.js'), 'utf8');
const m = src.match(/async function loadQuota\(\) \{[\s\S]*?\n\}\n/);

function run(paid, quota) {
  assert.ok(m, 'loadQuota not found');
  const calls = [];
  const cardCls = new Set();
  const card = { classList: { add: (c) => cardCls.add(c), remove: (c) => cardCls.delete(c) } };
  const ops = [];
  const el = (extra) => Object.assign({ textContent: '', style: {}, classList: { add: (c) => ops.push(`add ${c}`), remove: (c) => ops.push(`remove ${c}`) } }, extra);
  const els = {
    'tr-quota-text': el({ closest: (sel) => (sel === '.bg-white' ? card : null) }),
    'tr-quota-bar': el(), 'tr-quota-warning': el(),
  };
  const api = {
    isPaidEdition: async () => { if (paid instanceof Error) throw paid; return paid; },
    fetchApi: async (u) => { calls.push(u); return quota; },
  };
  const ctx = { api, document: { getElementById: (i) => els[i] || null },
    humanBytes: (n) => `${n}B`, console };
  vm.runInNewContext(`${m[0]}; globalThis.__f = loadQuota;`, ctx);
  return ctx.__f().then(() => ({ calls, hidden: cardCls.has('hidden'), text: els['tr-quota-text'].textContent, ops }));
}

for (const [name, paid] of [['ce', false], ['unknown/error', false]]) {
  test(`${name}: card hidden, no quota call`, async () => {
    const r = await run(paid, { used_bytes: 1, hard_quota_bytes: 10 });
    assert.deepStrictEqual(r.calls, []);
    assert.strictEqual(r.hidden, true);
    assert.strictEqual(r.text, '');
  });
}

test('paid (saas/pro): quota fetched and shown, card not hidden', async () => {
  const r = await run(true, { used_bytes: 5, hard_quota_bytes: 10 });
  assert.deepStrictEqual(r.calls, ['/harbor/tenant-quota']);
  assert.strictEqual(r.hidden, false);
  assert.match(r.text, /5B \/ 10B/);
});

test('paid keeps the >=80% unhide of tr-quota-warning (paid half of the CE pair)', async () => {
  const r = await run(true, { used_bytes: 9, hard_quota_bytes: 10 });
  assert.deepStrictEqual(r.calls, ['/harbor/tenant-quota']);
  assert.ok(r.ops.includes('remove hidden'));
});

// : the CE extract copy
// of both pages carries the new hide row's output on the <p id="tr-quota-warning"> opening tag
// (the row adds the HTML `hidden` attribute; ids untouched). Red if the manifest row is dropped
// or its replace stops adding the attribute.
const QROW = (require('node:fs').readFileSync(require('node:path').join(__dirname, '..', '..', '..', 'open-core', 'manifest.yaml'), 'utf8'));
const QFIND = '<p id="tr-quota-warning" class="mt-2 text-xs text-amber-600 hidden" hidden>';
const QREP = '<p id="tr-quota-warning" class="mt-2 text-xs text-amber-600 hidden" hidden>';
const QYFIND = QFIND.replace(/"/g, String.fromCharCode(92, 34));
const QYREP = QREP.replace(/"/g, String.fromCharCode(92, 34));
test('manifest hides tr-quota-warning paragraph on CE (row live + shape)', () => {
  assert.ok(QROW.includes(QYFIND) && QROW.includes(QYREP), 'hide row for p#tr-quota-warning missing/not adding hidden');
  const pages = ['pages/files.html', 'pages/tenant-registry.html'];
  for (const f of pages) {
    const src = require('node:fs').readFileSync(require('node:path').join(__dirname, '..', f), 'utf8');
    assert.strictEqual((src.match(QFIND) || []).length, 1, `${f}: paragraph tag not found once`);
  }
  void QFIND; void QREP;
});

test('real isPaidEdition is fail-closed (source pin)', () => {
  const apiSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'api.js'), 'utf8');
  assert.match(apiSrc, /ent\.edition === 'saas' \|\| ent\.edition === 'pro'/);
  assert.match(apiSrc, /if \(!ent \|\| ent\.error\) return false;/);
});
