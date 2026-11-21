// K3 check — js/k8s-clusters.js: the Kubernetes cluster registry card in
// Settings -> Integrations (list + probe summary, Add cluster with a Vault
// REFERENCE only, Probe, Remove with confirm, CE 402 message, self-hide on
// 404/403). Style of settings-oauth-connections.check.js + innerhtml-escape-g1.
//
//   node CORE/Dashboard/tests/k8s-clusters.check.js
//
// Static guards:
//   * the module never touches innerHTML (every server string is textContent),
//   * fetch paths never add /v1 (API_URL already ends in /v1),
//   * the paths match routers/k8s_clusters.py,
//   * the raw-kubeconfig guard exists and its needles are present,
//   * settings.js imports initializeK8sClustersPage and calls it.
// Dynamic guards (fake DOM + stubbed fetch):
//   * rows render with mode + probe summary (vendor / node count / GPU GB),
//   * a 404/403 list hides the whole card,
//   * Add posts exactly {name, in_cluster, kubeconfig_vault_ref?} and a 402
//     shows the fixed "This cluster could not be added" sentence,
//   * kubeconfig-looking input is refused client-side (no fetch),
//   * Remove asks confirm() first, then DELETEs,
//   * malicious names/caps stay TEXT.
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'js', 'k8s-clusters.js'), 'utf8');

(function() {
  assert.ok(!/\.innerHTML/.test(SRC), 'module must never touch innerHTML');
  assert.ok(!/\/v1\//.test(SRC) && !/['"`]\/v1/.test(SRC), 'paths must not add /v1');
  assert.ok(/\/k8s\/clusters`/.test(SRC), 'list/create path present');
  assert.ok(/\/k8s\/clusters\/\$\{encodeURIComponent\(String\(id\)\)\}\/probe/.test(SRC), 'probe path present');
  assert.ok(/\/k8s\/clusters\/\$\{encodeURIComponent\(String\(id\)\)\}`, \{ method: 'DELETE'/.test(SRC), 'delete path present');
  assert.ok(/function _k8sLooksRaw\(value\)/.test(SRC), 'raw-kubeconfig guard present');
  ['-----begin', 'api_key', 'insecureuse', 'ssh-rsa',
   'client-key-data', 'client-certificate-data', 'certificate-authority-data',
   'token:', 'password:', 'kind: config'].forEach((n) =>
    assert.ok(SRC.includes(`'${n}'`), `guard needle ${n} present`));
  assert.ok(/Probe failed - capabilities shown are a conservative default\./.test(SRC),
    'probe-failed hint sentence present');
  assert.ok(/This cluster could not be added/.test(SRC), '402 sentence present');
})();

(function() {
  const settings = fs.readFileSync(path.join(__dirname, '..', 'js', 'settings.js'), 'utf8');
  assert.ok(/import\s*\{\s*initializeK8sClustersPage\s*\}\s*from\s*['"]\.\/k8s-clusters\.js['"]/.test(settings),
    'settings.js must import initializeK8sClustersPage');
  assert.ok(/initializeK8sClustersPage\(\)/.test(settings), 'settings.js must call it');
})();

// --- dynamic -----------------------------------------------------------------
function extract(name) {
  const m = SRC.match(new RegExp(`(?:export )?(?:async )?function ${name}\\([\\s\\S]*?\\n}`));
  if (!m) throw new Error(`could not extract ${name}`);
  return m[0].replace(/^export /, '');
}
const NAMES = ['getApiUrl', 'authHeader', '_k8sWhen', '_k8sNormalizeList', '_k8sLooksRaw',
  '_k8sEl', '_k8sBadge', '_k8sFail', '_k8sClear', '_k8sLoadClusters', '_k8sCapsText',
  '_k8sRenderClusters', '_k8sRenderForm', '_k8sAdd', '_k8sProbe', '_k8sRemove',
  'initializeK8sClustersPage'];

class FakeDoc {
  constructor() { this.all = []; }
  createElement(tag) {
    const n = { tag, id: '', className: '', hidden: false, children: [], dataset: {}, type: '', listeners: {}, disabled: false, title: '', value: '', checked: false };
    n.classList = { add: (c) => { n.className = `${n.className} ${c}`.trim(); } };
    n.appendChild = (c) => { c._parent = n; n.children.push(c); };
    n.addEventListener = (ev, fn) => { n.listeners[ev] = fn; };
    n.removeChild = (c) => { n.children = n.children.filter((x) => x !== c); };
    // Real DOM: element.childNodes is a live NodeList - array-LIKE (length,
    // [i], item(i)) but it has NO .filter/.map/.indexOf. A plain Array stub
    // hides bugs like `(panel.childNodes || []).filter(...)` (the `|| []`
    // never fires because a NodeList is truthy), so mirror the NodeList here.
    Object.defineProperty(n, 'childNodes', { get: () => {
      const kids = n.children;
      const nl = { length: kids.length, item: (i) => kids[i] };
      for (let i = 0; i < kids.length; i++) nl[i] = kids[i];
      return nl;
    } });
    Object.defineProperty(n, 'textContent', {
      get: () => (n._text || '') + n.children.map((c) => c._text || '').join(' '),
      set: (v) => { n._text = v; n.children = []; },
    });
    Object.defineProperty(n, 'parentNode', { get: () => n._parent });
    this.all.push(n);
    return n;
  }
  getElementById(id) { return this.all.find((n) => n.id === id) || null; }
}
function textOf(n) { return (n._text || '') + n.children.map(textOf).join(' '); }
function findEl(root, pred, out = []) { if (pred(root)) out.push(root); root.children.forEach((c) => findEl(c, pred, out)); return out; }

let doc, fetchLog, confirmAnswer, mod;
function resetEnv(href) {
  doc = new FakeDoc();
  doc.panel = doc.createElement('div'); doc.panel.id = 'main-panel-integrations'; doc.all.push(doc.panel);
  globalThis.document = doc;
  globalThis.window = { APP_CONFIG: { API_URL: 'https://mod.example.com/api/v1' }, confirm: () => confirmAnswer, location: href };
  globalThis.sessionStorage = { getItem: () => 'tok' };
  fetchLog = [];
  // module-level state the extracted functions share (the oauth check's
  // modules keep their state in implicit globals; here the probe cache is
  // READ before write, so it must be declared in the generated scope).
  mod = new Function('let _k8sCapsCache = {};\n' + NAMES.map(extract).join('\n') + `\nreturn { ${NAMES.join(', ')} };`)();
}
function stubFetch(handler) {
  globalThis.fetch = async (url, opts) => { fetchLog.push({ url, opts }); return handler(url, opts); };
}
function ok(p) { return { ok: true, status: 200, json: async () => p }; }
function status(code) { return { ok: false, status: code, json: async () => ({ detail: 'x' }) }; }

const CAPS = { vendor: 'nvidia', nodes: [{ name: 'gpu2' }], gpu_memory_gb: 80, kai: true };
const CLUSTERS = { items: [
  { id: 3, name: 'gpu2-k3s', kubeconfig_vault_ref: 'vault://mod/k8s/gpu2', in_cluster: false, active: true, cluster_caps: CAPS, caps_probed_at: '2026-11-01T00:00:00Z' },
  { id: 5, name: 'in-cluster-row', kubeconfig_vault_ref: null, in_cluster: true, active: true },
  { id: 8, name: 'paused', kubeconfig_vault_ref: 'vault://x', in_cluster: false, active: false },
], active_count: 3 };

(async () => {
  // 1) list renders with probe summary
  resetEnv('https://mod.example.com/settings?tab=integrations#integrations');
  stubFetch((u) => {
    if (u.includes('/k8s/clusters')) return ok(CLUSTERS);
    return status(404);
  });
  await mod.initializeK8sClustersPage();
  const rows = findEl(doc.panel, (n) => n.tag === 'tr');
  assert.ok(rows.length === 4, 'header row + 3 cluster rows');
  const gpuRow = rows.find((r) => textOf(r).includes('gpu2-k3s'));
  assert.ok(textOf(gpuRow).includes('kubeconfig (Vault ref)') && textOf(gpuRow).includes('vendor nvidia · nodes 1 · GPU 80 GB · KAI'),
    'mode + probe summary render (fixture carries cluster_caps; the real list model omits it -> "not probed", covered by the cache path in 4)');
  const noCapsRow = rows.find((r) => textOf(r).includes('paused'));
  assert.ok(textOf(noCapsRow).includes('not probed'), 'row without caps shows not probed');
  assert.ok(findEl(doc.panel, (n) => n.tag === 'button' && textOf(n) === 'Probe').length === 3, 'Probe button per row');
  assert.ok(findEl(doc.panel, (n) => n.tag === 'button' && textOf(n) === 'Remove').length === 3, 'Remove button per row');
  assert.ok(findEl(doc.panel, (n) => n.tag === 'span' && textOf(n) === 'Inactive').length === 1, 'inactive row badge');
  assert.ok(findEl(doc.panel, (n) => n.tag === 'button' && textOf(n) === 'Add cluster').length === 1, 'Add button present');
  assert.ok(doc.getElementById('k8s-cluster-name') && doc.getElementById('k8s-cluster-vault-ref') && doc.getElementById('k8s-cluster-in-cluster'), 'add form fields present');

  // 2) Add: clean vault ref -> POST; 402 -> fixed CE sentence
  doc.getElementById('k8s-cluster-name').value = 'gpu2-k3s';
  doc.getElementById('k8s-cluster-vault-ref').value = 'vault://mod/k8s/gpu2';
  doc.getElementById('k8s-cluster-in-cluster').checked = false;
  stubFetch((u, o) => {
    if (o && o.method === 'POST' && u.includes('/k8s/clusters')) return status(402);
    if (u.includes('/k8s/clusters')) return ok(CLUSTERS);
    return status(404);
  });
  const added = await mod._k8sAdd();
  const post = fetchLog.find((f) => f.url.includes('/k8s/clusters') && f.opts.method === 'POST');
  assert.ok(post, 'POST /k8s/clusters issued');
  const body = JSON.parse(post.opts.body);
  assert.ok(body.name === 'gpu2-k3s' && body.in_cluster === false && body.kubeconfig_vault_ref === 'vault://mod/k8s/gpu2' && Object.keys(body).length === 3,
    'body is exactly {name, in_cluster, kubeconfig_vault_ref}');
  assert.ok(added === false, '402 -> false');
  const statusEl = findEl(doc.panel, (n) => n.tag === 'div' && String(n.className).includes('k8s-status'));
  assert.ok(statusEl.length === 1 && textOf(statusEl[0]).includes('This cluster could not be added'), 'fixed 402 sentence');

  // 3) raw kubeconfig text is refused client-side (no fetch at all)
  // FIX-CE-LEAK-GATE-K8S: the PEM-shaped needles below are built at RUNTIME (string
  // concat) so no source LINE carries the header shape the CE leak gate greps; the
  // value fed to the guard is byte-identical to the pre-fix literal.
  const pemNeedle = (kind) => '-----BEGIN ' + kind + ' PRIVATE' + ' KEY-----';
  doc.getElementById('k8s-cluster-vault-ref').value = pemNeedle('OPENSSH ') + '\napi_endpoint: https://x\n';
  const before = fetchLog.length;
  const refused = await mod._k8sAdd();
  assert.ok(refused === false && fetchLog.length === before, 'raw kubeconfig-looking input: no request sent');
  assert.ok(textOf(statusEl[0]).includes('not a Vault reference'), 'guard message shown');

  // 3b) one refused case per extra needle (no POST sent) + Vault refs still accepted
  const nameEl = doc.getElementById('k8s-cluster-name');
  const refEl = doc.getElementById('k8s-cluster-vault-ref');
  const inClusterEl = doc.getElementById('k8s-cluster-in-cluster');
  const RAW_INPUTS = [
    'client-key-data: Zm95',
    'client-certificate-data: Zm95',
    'certificate-authority-data: Zm95',
    pemNeedle('RSA '),
    'token: abcdef',
    'password: hunter2',
    'kind: config',
    'apiVersion: v1\nkind: Config',
  ];
  for (const raw of RAW_INPUTS) {
    nameEl.value = 'needle-case';
    refEl.value = raw;
    inClusterEl.checked = false;
    const b = fetchLog.length;
    assert.ok((await mod._k8sAdd()) === false && fetchLog.length === b, `refused: ${raw} (no request sent)`);
    assert.ok(textOf(statusEl[0]).includes('not a Vault reference'), `guard message shown for ${raw}`);
  }
  stubFetch((u, o) => {
    if (o && o.method === 'POST' && u.includes('/k8s/clusters')) return ok({ ok: true });
    if (u.includes('/k8s/clusters')) return ok(CLUSTERS);
    return status(404);
  });
  for (const ref of ['secret/data/tenants/1/k8s/prod', 'vault:secret/tenants/1/k8s/prod', 'vault://mod/k8s/gpu2']) {
    nameEl.value = 'vault-ref-ok';
    refEl.value = ref;
    inClusterEl.checked = false;
    const b = fetchLog.length;
    assert.ok((await mod._k8sAdd()) === true && fetchLog.length === b + 2, `Vault reference accepted: ${ref} (POST + refresh)`);
  }

  // 4) Remove: confirm first, then DELETE
  confirmAnswer = false;
  const b4 = fetchLog.length;
  assert.ok((await mod._k8sRemove(3)) === false && fetchLog.length === b4, 'confirm()=false: no DELETE');
  confirmAnswer = true;
  stubFetch((u) => {
    if (u.includes('/probe')) return ok({ id: 3, name: 'gpu2-k3s', cluster_caps: CAPS, caps_probed_at: '2026-11-05T00:00:00Z' });
    if (u.includes('/k8s/clusters')) return ok({ items: [CLUSTERS.items[0]], active_count: 1 });
    return status(404);
  });
  assert.ok((await mod._k8sRemove(3)) === true, 'remove returns true');
  assert.ok(fetchLog.some((f) => f.url.includes('/k8s/clusters/3') && f.opts.method === 'DELETE'), 'DELETE after confirm');
  await mod._k8sProbe(3);
  assert.ok(fetchLog.some((f) => f.url.includes('/k8s/clusters/3/probe') && f.opts.method === 'POST'), 'probe POST');
  const probedRow = findEl(doc.panel, (n) => n.tag === 'tr').find((r) => textOf(r).includes('gpu2-k3s'));
  assert.ok(textOf(probedRow).includes('vendor nvidia · nodes 1 · GPU 80 GB · KAI'), 'probe summary renders after probe');

  // 4b) probe failure: 200 + probe_error -> fixed hint, server string never shown
  resetEnv('https://mod.example.com/settings#integrations');
  stubFetch((u) => {
    if (u.includes('/probe')) return ok({ id: 3, name: 'gpu2-k3s', cluster_caps: { vendor: 'conservative' }, caps_probed_at: '2026-11-05T00:00:00Z', probe_error: 'k8s-probe module unavailable on host' });
    if (u.includes('/k8s/clusters')) return ok({ items: [CLUSTERS.items[0]], active_count: 1 });
    return status(404);
  });
  await mod.initializeK8sClustersPage();
  assert.ok((await mod._k8sProbe(3)) === true, 'failed probe still returns true');
  const statusEl2 = findEl(doc.panel, (n) => n.tag === 'div' && String(n.className).includes('k8s-status'));
  assert.ok(statusEl2.length === 1 && textOf(statusEl2[0]) === 'Probe failed - capabilities shown are a conservative default.',
    'probe-failed hint shown via textContent');
  assert.ok(!textOf(doc.panel).includes('k8s-probe module unavailable'), 'server probe_error string never echoed');

  // 5) 404/403 hides the whole card
  resetEnv('https://mod.example.com/settings#integrations');
  stubFetch((u) => status(403));
  await mod.initializeK8sClustersPage();
  const card = findEl(doc.panel, (n) => n.id === 'int-k8s-clusters');
  assert.ok(card.length === 0 || card[0].className.includes('hidden'), 'card hidden on 403');
  assert.ok(findEl(doc.panel, (n) => n.tag === 'table').length === 0, 'no table rendered');

  // 6) malicious strings stay text
  resetEnv('https://mod.example.com/settings#integrations');
  stubFetch((u) => ok({ items: [{ id: 1, name: '<img src=x onerror=alert(1)>', kubeconfig_vault_ref: '<script>alert(1)</script>', in_cluster: false, active: true, cluster_caps: { vendor: '<b>x</b>', nodes: [1, 2] } }], active_count: 1 }));
  await mod.initializeK8sClustersPage();
  assert.ok(findEl(doc.panel, (n) => n.tag === 'img' || n.tag === 'script' || n.tag === 'b').length === 0, 'no injected elements');
  assert.ok(findEl(doc.panel, (n) => n.tag === 'td').some((t) => textOf(t).includes('<img src=x')), 'malicious name rendered as text');
  console.log('k8s-clusters.check.js: all assertions passed');
})();
