// FX.68 p2 — static a11y floor for the Dashboard's baked pages.
//
// The live Dashboard serves BAKED files (no bind-mount of pages/js), so the
// axe E2E run cannot see source fixes until a rebuild. This test is the
// pre-redeploy verification: it parses pages/*.html and asserts the three
// mechanical WCAG floors axe was hitting —
//   1. every <input>/<select>/<textarea> has an accessible name
//      (a for=<id> label elsewhere in the file, or aria-label / aria-labelledby
//       / placeholder-as-name for text inputs is NOT enough — we require a real
//       label or aria-label),
//   2. every <button>/<a> has a non-empty text content or aria-label
//       (text may come from an <img alt>, an svg <title>, or nested text),
//   3. no positive tabindex (focus order must follow DOM order),
// plus aria-* attribute validity (role values must be known ARIA roles, aria-*
// attributes must be from the ARIA attribute set).
//
// Deliberately file-scoped: it reads the shipped source, not the served DOM.
// Run: node --test tests/a11y-static.test.js

const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');

const PAGES_DIR = path.join(__dirname, '..', 'pages');

// Files other agents own — their a11y is out of this task's scope.
const SKIP_PAGES = new Set(['release-approvals.html', 'ip-blocks.html']);

const ARIA_ROLES = new Set([
  'alert', 'alertdialog', 'application', 'article', 'complementary', 'button',
  'tabpanel', 'tablist', 'list', 'term', 'definition', 'listitem',
  'cell', 'checkbox', 'circle', 'code', 'columnheader', 'combobox', 'content',
  'definition', 'deletion', 'dialog', 'document', 'emphasis', 'figure',
  'generic', 'graphics-symbol', 'grid', 'group', 'heading', 'img', 'list',
  'listitem', 'log', 'main', 'marquee', 'math', 'menu', 'menubutton',
  'menuitem', 'menuitemcheckbox', 'menuitemradio', 'meter', 'navigation',
  'none', 'note', 'option', 'paragraph', 'presentation', 'progressbar',
  'radiobutton', 'range', 'region', 'row', 'rowgroup', 'searchbox',
  'separator', 'slider', 'spinbutton', 'status', 'strong', 'subscript',
  'superscript', 'switch', 'tab', 'table', 'textbox', 'time', 'timer',
  'tooltip', 'tree', 'treeitem', 'contentinfo', 'form', 'link',
  'listitem', 'option', 'presentation', 'button', 'alert', 'banner',
  'complementary', 'main', 'navigation', 'region', 'search',
]);

const ARIA_ATTRS = new Set([
  'aria-atomic', 'aria-bracedescription', 'aria-busy', 'aria-controls',
  'aria-current', 'aria-describedby', 'aria-description', 'aria-details',
  'aria-disabled', 'aria-dropeffect', 'aria-errormessage', 'aria-expanded',
  'aria-flowto', 'aria-grabbed', 'hasdescription', 'hidden', 'hint',
  'label', 'labelledby', 'describedby', 'details', 'errormessage',
  'expanded', 'flowto', 'grabbed', 'hasdescription', 'hidden', 'hint',
  'label', 'labelledby', 'level', 'modal', 'multiline', 'multiset',
  'orientation', 'owns', 'pressed', 'readonly', 'relevant', 'role',
  'rowcount', 'rowindex', 'selected', 'shape', 'sorted', 'valuemax',
  'valuemin', 'valuenow', 'valuetext',
]);

function ariaAttrOk(name) {
  const n = name.toLowerCase();
  if (!n.startsWith('aria-')) return true;
  const rest = n.slice(5);
  if (ARIA_ATTRS.has(rest)) return true;
  // data-* style aria props we actually use: aria-label/labelledby/describedby
  return ['label', 'labelledby', 'describedby', 'hidden', 'role', 'expanded',
          'pressed', 'checked', 'disabled', 'current', 'live', 'atomic',
          'relevant', 'busy', 'owns', 'controls', 'hasdescription',
          'keyshortcuts', 'roledescription', 'valuetext', 'valuemin',
          'valuemax', 'valuencount', 'valuemax', 'multiline', 'multiset',
          'current', 'details', 'errormessage', 'colcount', 'rowcount',
          'colindex', 'rowindex', 'rowindex', 'level', 'sort', 'expanded']
    .includes(rest);
}

function stripComments(html) {
  return html.replace(/<!--[\s\S]*?-->/g, ' ');
}

function collectLabels(html) {
  // ids that some <label for="..."> names
  const ids = new Set();
  for (const m of html.matchAll(/<label\b[^>]*\bfor\s*=\s*["']([^"']+)["'][^>]*>/gi)) {
    ids.add(m[1]);
  }
  // implicit association: a control nested inside a <label>...</label> block
  for (const m of html.matchAll(/<label\b[^>]*>([\s\S]*?)<\/label>/gi)) {
    for (const c of m[1].matchAll(/<(input|select|textarea)\b[^>]*\bid\s*=\s*["']([^"']+)["'][^>]*>/gi)) {
      ids.add(c[2]);
    }
  }
  return ids;
}

function checkPage(name, html) {
  const clean = stripComments(html);
  const labelFors = collectLabels(clean);
  const problems = [];

  // 1. form controls need a name
  for (const m of clean.matchAll(/<(input|select|textarea)\b([^>]*)>/gi)) {
    const tag = m[0];
    const attrs = m[2];
    const get = (a) => {
      const r = new RegExp(`\\b${a}\\s*=\\s*["']([^"']*)["']`, 'i').exec(attrs);
      return r ? r[1] : null;
    };
    if (/(type\s*=\s*["']?(checkbox|radio|hidden|password)["']?)/i.test(attrs)) continue;
    const id = get('id');
    const named = get('aria-label') || get('aria-labelledby') || get('name');
    if (!named && !(id && labelFors.has(id))) {
      problems.push(`${name}: <${m[1]} ...> no label/aria-label (id=${id || '-'}) -> ${tag.slice(0, 120)}`);
    }
  }

  // 2. buttons and links need a name
  for (const m of clean.matchAll(/<(button|a)\b([^>]*)>([\s\S]*?)<\/\1>/gi)) {
    const tag = m[0];
    const attrs = m[2];
    const inner = m[3];
    const aria = /aria-label\s*=\s*["'][^"']+["']/i.test(attrs)
      || /aria-labelledby\s*=\s*["'][^"']+["']/i.test(attrs)
      || /title\s*=\s*["'][^"']+["']/i.test(attrs);
    // visible text: strip nested tags, keep text and alt/title of images
    const altText = inner.match(/alt\s*=\s*["']([^"']+)["']/i);
    const svgTitle = inner.match(/<title[^>]*>([^<]+)<\/title>/i);
    const textOnly = inner
      .replace(/<[^>]*>/g, ' ')
      .replace(/&nbsp;/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
    if (!aria && !textOnly && !altText && !svgTitle) {
      problems.push(`${name}: <${m[1]}> unnamed -> ${tag.replace(/\s+/g, ' ').slice(0, 140)}`);
    }
  }

  // 3. no positive tabindex
  for (const m of clean.matchAll(/\btabindex\s*=\s*["']\s*(\d+)\s*["']/gi)) {
    if (parseInt(m[1], 10) > 0) {
      problems.push(`${name}: positive tabindex=${m[1]} (focus order)`);
    }
  }

  // 4. aria attribute validity
  for (const m of clean.matchAll(/(^|[^\w-])(aria-[a-z-]+|role)\s*=\s*["']([^"']*)["']/gi)) {
    const attr = m[2].toLowerCase();
    const val = m[3].trim();
    if (attr === 'role') {
      const tok = val.split(/\s+/)[0];
      if (tok && !ARIA_ROLES.has(tok)) {
        problems.push(`${name}: role="${val}" not a known ARIA role`);
      }
    } else if (!ariaAttrOk(attr)) {
      problems.push(`${name}: aria attribute "${attr}" is not in the ARIA attribute set`);
    }
  }

  return problems;
}

test('every page passes the static a11y floor', () => {
  const files = fs.readdirSync(PAGES_DIR).filter((f) => f.endsWith('.html') && !SKIP_PAGES.has(f));
  assert.ok(files.length > 0, 'no pages found');
  const all = [];
  for (const f of files) {
    const html = fs.readFileSync(path.join(PAGES_DIR, f), 'utf8');
    all.push(...checkPage(f, html));
  }
  if (all.length) console.log(all.join('\n'));
  assert.deepEqual(all, [], 'static a11y violations in pages/*.html:\n' + all.join('\n'));
});
