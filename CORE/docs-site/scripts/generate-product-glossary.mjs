#!/usr/bin/env node
/**
 * generate-product-glossary.mjs — OZ-E8: generate the product-names glossary
 * page for the docs site from the Oz theming display map.
 *
 * ONE source of truth: CORE/Dashboard/js/product-names.js (OZ-A1, 28 keys) —
 * the Python mirror CORE/API/product_names.py (OZ-A2) is parsed too and must
 * agree field-for-field, so a one-sided edit fails the build instead of
 * shipping a half-renamed page. The names come from
 * openspec/changes/oz-theming/NAME-MAP.md (owner-fixed, 28 products).
 *
 * The map is ADDITIVE. This generator never renames anything: page ids, slugs,
 * hrefs, API routes, env names and worker image names are taken from the maps
 * and are asserted unchanged (the generated page carries no ids/routes at all,
 * only display names). `ids-changed=0` is proven by --check.
 *
 * MOD_THEME_NAMES (OZ-A3) works exactly as it does in the Dashboard and the
 * API: OFF (env = 0/off/false/no) renders today's literal product words
 * byte for byte — no codenames anywhere — ON (default in CE and paid alike)
 * renders the map label. A key with no label falls back to today's literal.
 *
 * A PAID-only row (`paidOnly`) keeps its own original wording and is marked
 * "Paid editions only"; CE hides the feature itself. `--ce` drops that row
 * (27 entries) for a CE-only build if Tim ever orders it.
 *
 * Output: markdown on stdout (the CI step and `npm run` redirect it to
 * CORE/docs-site/docs/product-names.md); a one-line summary on stderr.
 * `--check <file>` regenerates and compares: exit 1 on drift, so CI fails a
 * hand-edited page. Deterministic: no timestamps, map order only.
 */

import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const REPO_ROOT = join(__dirname, '..', '..', '..');
const JS_MAP = join(REPO_ROOT, 'CORE', 'Dashboard', 'js', 'product-names.js');
const PY_MAP = join(REPO_ROOT, 'CORE', 'API', 'product_names.py');
const PAGE = join(REPO_ROOT, 'CORE', 'docs-site', 'docs', 'product-names.md');

const EXPECTED_COUNT = 28;
const TRUTHY_OFF = new Set(['0', 'off', 'false', 'no']);

/**
 * Today's literal product words (what the docs and sidebar call each zone
 * with no theming). Keyed by the internal id, which is NEVER printed.
 */
const TODAY = {
  'mod-core': 'MOD',
  'plane': 'Voice',
  'workflow-builder': 'Workflow Builder',
  'wizards': 'Wizards',
  'converter': 'Converter',
  'assistant-launcher': 'Help chat',
  'workflows': 'Workflows',
  'compliance-center': 'Compliance',
  'capture-devices': 'Capture devices',
  'marketplace': 'Marketplace',
  'triggers': 'Triggers',
  'dashboard-shell': 'Dashboard',
  'vector_store': 'RAG ingest',
  'deploy': 'Deploy',
  'stacks': 'Stacks',
  'workers': 'Workers',
  'agents': 'Agents',
  'managers': 'Manager',
  'scheduler': 'Scheduler',
  'burst-accounts': 'Cloud burst',
  'lockbox': 'Secrets',
  'admin-sso': 'Sign-in',
  'node-builder': 'Node builder',
  'files': 'Files',
  'sleep-back': 'Model sleep-back',
  'security': 'Security',
  'image-mover': 'Image and model mover',
  'burst': 'Rainbow MOD Cloud',
};

function parseJs(text) {
  if (!/export const PRODUCT_NAMES/.test(text)) {
    throw new Error('product-names.js must export PRODUCT_NAMES');
  }
  const entryRe = /'([a-z0-9_-]+)'\s*:\s*\{([^{}]*)\}/g;
  // Same delimiter via backreference so an apostrophe inside a double-quoted
  // value ("Burst jobs to MOD's hosted cloud") parses.
  const fieldRe = /(codename|label|subtitle)\s*:\s*(["'])((?:(?!\2).)*)\2/g;
  const out = new Map();
  let m;
  while ((m = entryRe.exec(text))) {
    const body = m[2];
    const rec = {};
    let f;
    while ((f = fieldRe.exec(body))) rec[f[1]] = f[3];
    out.set(m[1], { ...rec, paidOnly: /paidOnly\s*:\s*true/.test(body) });
  }
  return out;
}

function parsePy(text) {
  if (!/PRODUCT_NAMES:\s*dict/.test(text)) {
    throw new Error('product_names.py must declare PRODUCT_NAMES');
  }
  const entryRe = /"([a-z0-9_-]+)"\s*:\s*\{([^{}]*)\}/g;
  const fieldRe = /"(codename|label|subtitle)"\s*:\s*"((?:[^"]*))"/g;
  const out = new Map();
  let m;
  while ((m = entryRe.exec(text))) {
    const body = m[2];
    const rec = {};
    let f;
    while ((f = fieldRe.exec(body))) rec[f[1]] = f[2];
    out.set(m[1], { ...rec, paidOnly: /["']?paidOnly["']?\s*:\s*True/.test(body) });
  }
  return out;
}

function agree(js, py) {
  const problems = [];
  if (js.size !== py.size) problems.push(`key count js=${js.size} py=${py.size}`);
  for (const [k, v] of js) {
    const w = py.get(k);
    if (!w) { problems.push(`key ${k} missing on the Python side`); continue; }
    for (const f of ['codename', 'label', 'subtitle']) {
      if ((v[f] || '') !== (w[f] || '')) problems.push(`${k}.${f} differs`);
    }
    if (v.paidOnly !== w.paidOnly) problems.push(`${k}.paidOnly differs`);
  }
  for (const k of py.keys()) if (!js.has(k)) problems.push(`key ${k} missing on the JS side`);
  return problems;
}

function themeEnabled(env) {
  const flag = env.MOD_THEME_NAMES;
  if (flag === undefined || flag === '') return true;
  return !TRUTHY_OFF.has(String(flag).trim().toLowerCase());
}

function render(map, enabled, dropPaid) {
  const rows = [];
  for (const [key, entry] of map) {
    if (dropPaid && entry.paidOnly) continue;
    const today = TODAY[key] || key;
    if (!enabled) {
      rows.push({ heading: today, entry, key });
      continue;
    }
    rows.push({ heading: entry.label || entry.codename || today, entry, key });
  }
  const lines = [
    '---',
    'title: Product names',
    'sidebar_position: 4',
    'slug: /product-names',
    'description: Every product in MOD in plain language, with the name you see in the sidebar.',
    '---',
    '',
    '# Product names',
    '',
    'MOD groups its features into products. Each one has a short, memorable',
    'name in the sidebar and a plain-language line saying what it does. The',
    'table below is the whole set — nothing here changes a page address, a',
    'route, an environment variable or a worker image name.',
    '',
    `<!-- products: ${rows.length} -->`,
    '',
    '## The list',
    '',
  ];
  for (const r of rows) {
    lines.push(`### ${r.heading}`);
    lines.push('');
    lines.push(`${r.entry.subtitle}.`);
    lines.push('');
    if (r.entry.paidOnly) {
      lines.push('*Paid editions only.*');
      lines.push('');
    }
  }
  lines.push('## Related');
  lines.push('');
  lines.push('- [Glossary](glossary.md) — the plain-English words used all over MOD.');
  lines.push('');
  return lines.join('\n');
}

function main() {
  const argv = process.argv.slice(2);
  const dropPaid = argv.includes('--ce');
  const ci = argv.indexOf('--check');
  const js = parseJs(readFileSync(JS_MAP, 'utf8'));
  const py = parsePy(readFileSync(PY_MAP, 'utf8'));
  const problems = agree(js, py);
  if (problems.length) {
    process.stderr.write(`map drift: ${problems.length} problem(s): ${problems.join('; ')}\n`);
    process.exit(2);
  }
  if (js.size !== EXPECTED_COUNT) {
    process.stderr.write(`expected ${EXPECTED_COUNT} products, found ${js.size}\n`);
    process.exit(2);
  }
  for (const [k, v] of js) {
    for (const f of ['codename', 'label', 'subtitle']) {
      if (!v[f] || !String(v[f]).trim()) {
        process.stderr.write(`entry ${k} has a blank ${f}\n`);
        process.exit(2);
      }
    }
  }
  const out = render(js, themeEnabled(process.env), dropPaid);
  if (ci !== -1) {
    const target = argv[ci + 1] || PAGE;
    const current = readFileSync(target, 'utf8');
    if (current !== out) {
      process.stderr.write(`glossary page is stale: regenerate ${target}\n`);
      process.exit(1);
    }
    process.stderr.write(`product-names page up to date (${js.size} products, 0 drift)\n`);
    return;
  }
  process.stdout.write(out);
  process.stderr.write(
    `generated ${js.size} products (themed=${themeEnabled(process.env) ? 'on' : 'off'}, paid rows ${dropPaid ? 'dropped' : 'kept'})\n`
  );
}

main();
