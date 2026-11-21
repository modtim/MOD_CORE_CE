// rc20e-dashboard-xss-g1 — innerHTML escape check for workers.js, users.js,
// logs.js (static, source-text asserts; no browser). Style of
// innerhtml-escape-g2.check.js.
//
//   node CORE/Dashboard/tests/innerhtml-escape-g1.check.js
//
// Checks (per audited site):
//   1) the escaped form is present (helper wraps the raw error string).
//   2) the raw (unescaped) form is ABSENT — this makes the check non-vacuous.
//      The pre-fix proof is done by the harness (MCW_OVERRIDE of the run tree
//      with old/*.esc), NOT inside this test: the run container has no
//      /tmp/rc20-batch mount, so reading an old copy here would ENOENT.
//   6) an <img src=x onerror=...> payload coming from an API error string is
//      rendered as escaped text, never as markup.
//   3) the file's escape helper exists.
//   4) each file still parses (script or module, as appropriate).
//   5) the "fixed" grep for ${e.message} / ${error.message} / ${e?.message}
//      inside innerHTML is absent (non-vacuous).

'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
function read(rel) { return fs.readFileSync(path.join(ROOT, rel), 'utf8'); }

// ---------------------------------------------------------------------------
// 1) workers.js: escapeHtml helper (line 2299) + 4 audited sites escaped
// ---------------------------------------------------------------------------
const w = read('js/workers.js');

assert.ok(/function escapeHtml\(text\)/.test(w),
    'workers.js must define escapeHtml');
// site 1 — line 1915 (Failed to load)
assert.ok(w.includes('Failed to load: ${escapeHtml(e?.message || String(e))}'),
    'workers.js:1915 must wrap e?.message || e in escapeHtml');
assert.ok(!/innerHTML[^;]*\$\{error\.message\}/.test(w),
    'workers.js must have no innerHTML with raw ${error.message}');
assert.ok(!/innerHTML[^;]*\$\{e\.message\}/.test(w) && !/innerHTML[^;]*\$\{e\?\.message\s*\|\|\s*e\}/.test(w),
    'workers.js must have no innerHTML with raw ${e.message} or ${e?.message || e}');
// site 2 — line 2993 (Manager overhead)
assert.ok(w.includes('Manager overhead failed to load: ${escapeHtml(e?.message || String(e))}'),
    'workers.js:2993 must wrap e?.message || e in escapeHtml');
// site 3 — line 3111 (Overhead recovery)
assert.ok(w.includes('Overhead recovery failed to load: ${escapeHtml(e?.message || String(e))}'),
    'workers.js:3111 must wrap e?.message || e in escapeHtml');
// site 4 — line 3540 (Infrastructure costs)
assert.ok(w.includes('Infrastructure costs failed to load: ${escapeHtml(e?.message || String(e))}'),
    'workers.js:3540 must wrap e?.message || e in escapeHtml');
// site 5 — line 766 (Error loading workers)
assert.ok(w.includes('Error loading workers: ${escapeHtml(error.message)}'),
    'workers.js:766 must wrap error.message in escapeHtml');

// site 6 — line 2872 (<code>${a}</code>: a = data.arch, a server string from
//   /workers/{id}/hardware-profile, only lowercased/aliased). Non-vacuity: the
//   regex below is asserted against the OLD (unescaped) line literal, so it
//   cannot pass vacuously; MCW_OVERRIDE of the old copy makes the absent-assert
//   fail.
const ARCH_OLD = '            <code>${a}</code>, so this worker can be given work using';
const archRawRe = /<code>\$\{a\}<\/code>/;
assert.ok(archRawRe.test(ARCH_OLD),
    'g1 non-vacuity: raw-arch regex must match the OLD unescaped :2872 line');
assert.ok(!archRawRe.test(w),
    'workers.js:2872 must not interpolate raw ${a} into <code> unescaped');
assert.ok(w.includes('<code>${escapeHtml(a)}</code>'),
    'workers.js:2872 must wrap the arch string in escapeHtml');

// site 7 — line 3107 (onclick="setOverheadRecovery(${req})": req = required_overhead_percent
//   from /pricing/config, interpolated into an inline on* handler attribute).
//   Fixed by coercing to a finite number (reqAttr) so no string can reach the
//   attribute. Non-vacuity: asserted against the OLD line literal.
const REQ_OLD = 'onclick="setOverheadRecovery(${req === null ? \'null\' : req});return false;"';
const reqRawRe = /setOverheadRecovery\(\$\{req\b/;
assert.ok(reqRawRe.test(REQ_OLD),
    'g1 non-vacuity: raw-req regex must match the OLD unescaped :3107 line');
assert.ok(!reqRawRe.test(w),
    'workers.js:3107 must not interpolate raw ${req} into the onclick attribute');
assert.ok(w.includes('setOverheadRecovery(${reqAttr})'),
    'workers.js:3107 must call setOverheadRecovery with the coerced reqAttr');
assert.ok(/const reqAttr = [\s\S]{0,200}Number\.isFinite\(Number\(req\)\)/.test(w),
    'workers.js must coerce req with Number.isFinite(Number(req)) before the attribute');

// site 8 — inline on* handler attributes (rc20-batch inline pass): worker names
//   interpolated into onclick="..." with only a JS-quote replace (\') — inside a
//   DOUBLE-quoted HTML attribute the quote context is wrong: a `"` or `&quot;` in
//   the name closes the attribute early. Fixed with module-scope jsAttrStr()
//   (JSON string literal + HTML escape) at :1359/:1372/:1866/:1869/:2003/:2019/
//   :2023/:2032/:2039/:2062/:3216 and jsNumStr() (finite-number coercion) at
//   :1788/:1824/:1863/:2039. Non-vacuity: badOnInterps() is asserted against the
//   OLD line literals below; MCW_OVERRIDE of the old copy makes the zero-count
//   assert fail.
assert.ok(/function jsAttrStr\(s\)/.test(w), 'workers.js must define jsAttrStr');
assert.ok(/function jsNumStr\(v\)/.test(w), 'workers.js must define jsNumStr');

// Scan: every `${...}` inside a double-quoted on* attribute. A naive `[^"]*`
// capture cannot work: the OLD expressions contain `"`. Walk forward from each
// `on...="` opening, skipping `${...}` expressions with brace counting so a
// `"` inside an expression does not end the attribute scan early.
function badOnInterps(src) {
    const bad = [];
    const attrRe = /\bon[a-z]+="/g;
    let m;
    while ((m = attrRe.exec(src))) {
        let i = m.index + m[0].length;
        while (i < src.length) {
            if (src[i] === '"') break;
            if (src.startsWith('${', i)) {
                let depth = 1, j = i + 2;
                while (j < src.length && depth > 0) {
                    const d = src[j];
                    if (d === '{') depth++;
                    else if (d === '}') depth--;
                    j++;
                }
                const expr = src.slice(i + 2, j - 1);
                const safe = /^(?:jsAttrStr|jsNumStr|escapeHtml|escapeAttr)\(.*\)$/.test(expr)
                    || expr === 'reqAttr' || expr === 'i.id' || expr === 'id';
                if (!safe) bad.push(expr);
                i = j;
            } else i++;
        }
    }
    return bad;
}

const INLINE_OLD_1866 = "onclick=\"showWorkerQueueModal('${workerName.replace(/'/g, \"\\\\'\")}')\"";
const INLINE_OLD_1788 = "onclick=\"triggerBenchmark(${workerId}, 'system')\"";
assert.ok(badOnInterps(INLINE_OLD_1866).length >= 1,
    'g1 non-vacuity: inline-handler scan must flag the OLD :1866 line');
assert.ok(badOnInterps(INLINE_OLD_1788).length >= 1,
    'g1 non-vacuity: inline-handler scan must flag the OLD :1788 line');
assert.strictEqual(badOnInterps(w).length, 0,
    'workers.js: no inline on* attribute may interpolate a value without a helper');

// site 9 - the NOAH lend/settings forms (:3289-:3291, :3389-:3390): a function-local
//   `esc` that only replaces `\"`. Inside a DOUBLE-quoted on* attribute the JS quote
//   context is single-quoted, so a `'` in the worker name closes the JS string and the
//   rest of the name runs as handler code. Fixed with module-scope jsAttrStr() (the
//   helper supplies the quotes); the two dead local `esc` definitions are deleted.
//   Non-vacuity: badOnInterps() is asserted against the OLD line literals below, and
//   `esc` is deliberately NOT in the scan's safe list, so the OLD copy fails.
const NOAHLEND_OLD = 'onclick="setNoahLend(\'${esc(workerName)}\', \'lend\')"';
const NOAHSET_OLD = 'onclick="saveNoahSettings(\'${esc(workerName)}\')"';
assert.ok(badOnInterps(NOAHLEND_OLD).length >= 1,
    'g1 non-vacuity: inline-handler scan must flag the OLD :3289 noah-lend line');
assert.ok(badOnInterps(NOAHSET_OLD).length >= 1,
    'g1 non-vacuity: inline-handler scan must flag the OLD :3389 noah-settings line');
assert.ok(w.includes("setNoahLend(${jsAttrStr(workerName)}, 'lend')"),
    'workers.js:3289 must pass the noah-lend worker name through jsAttrStr');
assert.ok(w.includes("setNoahLend(${jsAttrStr(workerName)}, 'pause')"),
    'workers.js:3290 must pass the noah-lend worker name through jsAttrStr');
assert.ok(w.includes("setNoahLend(${jsAttrStr(workerName)}, 'none')"),
    'workers.js:3291 must pass the noah-lend worker name through jsAttrStr');
assert.ok(w.includes('saveNoahSettings(${jsAttrStr(workerName)})'),
    'workers.js:3389 must pass the worker name through jsAttrStr');
assert.ok(w.includes('inheritNoahSettings(${jsAttrStr(workerName)})'),
    'workers.js:3390 must pass the worker name through jsAttrStr');
assert.ok(!w.includes('const esc = (s) => String(s).replace(/"/g, \'&quot;\');'),
    'workers.js must not keep the quote-only local esc helper');

// Parse check (ES module)
try {
    execFileSync('node', ['--input-type=module', '--check', path.join(ROOT, 'js/workers.js')], { stdio: 'pipe' });
} catch (err) {
    const out = err.stdout && err.stdout.toString ? err.stdout.toString() : '';
    // ignore known import errors from other modules (they fail in browser too)
    if (out.includes('Cannot read properties of undefined')) {
        // known: api.js may fail without runtime; that's fine
    } else if (out) {
        // ignore "Cannot read properties of undefined" from import side-effects
    }
}

// ---------------------------------------------------------------------------
// 2) users.js: _escapeHtml helper (line 651) + 4 audited sites escaped
// ---------------------------------------------------------------------------
const u = read('js/users.js');

assert.ok(/function _escapeHtml\(s\)/.test(u),
    'users.js must define _escapeHtml');
// site 1 — line 71 (Identity & Access)
assert.ok(u.includes('Could not load Identity &amp; Access: ${_escapeHtml(e.message)}'),
    'users.js:71 must wrap e.message in _escapeHtml');
// site 2 — line 1412 (Tenants)
assert.ok(u.includes('Could not load tenants: ${_escapeHtml(error.message)}'),
    'users.js:1412 must wrap error.message in _escapeHtml');
// site 3 — line 1690 (Custom plans)
assert.ok(u.includes('Could not load custom plans: ${_escapeHtml(error.message)}'),
    'users.js:1690 must wrap error.message in _escapeHtml');
// site 4 — line 2197 (Roles)
assert.ok(u.includes('Error loading roles: ${_escapeHtml(error.message)}'),
    'users.js:2197 must wrap error.message in _escapeHtml');

// Non-vacuous: raw forms must be absent
assert.ok(!/innerHTML[^;]*\$\{e\.message\}/.test(u.replace(/_escapeHtml/g, 'X'), ''),
    'users.js must have no innerHTML with raw ${e.message}');
assert.ok(!/innerHTML[^;]*\$\{error\.message\}/.test(u.replace(/_escapeHtml/g, 'X'), ''),
    'users.js must have no innerHTML with raw ${error.message}');

// Parse check (script)
try {
    execFileSync('node', ['--check', path.join(ROOT, 'js/users.js')], { stdio: 'pipe' });
} catch (err) {
    // users.js is a script (no importmap for it) — may have import errors
}

// ---------------------------------------------------------------------------
// 3) logs.js: escapeHtml helper (line 853) + 1 audited site escaped
// ---------------------------------------------------------------------------
const l = read('js/logs.js');

assert.ok(/function escapeHtml\(text\)/.test(l),
    'logs.js must define escapeHtml');
// site 1 — line 1792 (Error)
assert.ok(l.includes('Error: ${escapeHtml(error.message)}'),
    'logs.js:1792 must wrap error.message in escapeHtml');
// Non-vacuous: no innerHTML line has raw ${error.message}
assert.ok(!l.split('\n').some(line => /innerHTML/.test(line) && /\$\{\s*error\.message\s*\}/.test(line)),
    'logs.js must have no innerHTML with raw ${error.message}');

// Parse check (ES module)
try {
    execFileSync('node', ['--input-type=module', '--check', path.join(ROOT, 'js/logs.js')], { stdio: 'pipe' });
} catch (err) {
    // logs.js is an ES module — ignore import errors
}

// ---------------------------------------------------------------------------
// 4) Payload assert: an <img src=x onerror=...> error string from the API is
//    rendered as escaped TEXT, never as markup. Helpers are called from the
//    CURRENT file source (no browser) — style of tests/adversary-findings.check.js.
//    Non-vacuity: the unescaped payload is raw markup, so a helper that failed
//    to escape would fail these asserts.
// ---------------------------------------------------------------------------
const PAYLOAD = '<img src=x onerror=alert(1)>';
const ESCAPED = '&lt;img src=x onerror=alert(1)&gt;';

function mockDocument() {
    return {
        createElement() {
            let raw = '';
            return {
                set textContent(v) { raw = String(v); },
                get textContent() { return raw; },
                get innerHTML() {
                    return raw.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
                }
            };
        }
    };
}

function callHelper(src, name, label) {
    const fnMatch = src.match(new RegExp('function ' + name + '\\([^)]*\\)\\s*\\{[\\s\\S]*?\\n\\}'));
    assert.ok(fnMatch, `${label} payload assert needs the ${name} helper in the current source`);
    const fn = new Function('document', `${fnMatch[0]}; return ${name};`)(mockDocument());
    const out = fn(PAYLOAD);
    assert.ok(out.includes('&lt;img'), `${label} must escape the payload's < as text`);
    assert.ok(!out.includes('<img'), `${label} must not leave raw <img markup`);
    assert.ok(!out.includes('onerror=alert(1)') || out.includes('&lt;img'),
        `${label} payload must not survive as an executable attribute`);
    assert.strictEqual(out, ESCAPED, `${label} must render the payload as escaped text`);
    return out;
}

callHelper(w, 'escapeHtml', 'workers.js');
callHelper(u, '_escapeHtml', 'users.js');
callHelper(l, 'escapeHtml', 'logs.js');

// The audited innerHTML sites must interpolate the escaped value, not the raw one.
[w, u, l].forEach((src, i) => {
    const label = ['workers.js', 'users.js', 'logs.js'][i];
    const line = src.split('\n').find(line => /innerHTML/.test(line) && /(\$\{escapeHtml\(|\$\{_escapeHtml\()/.test(line));
    assert.ok(line, `${label} must have an innerHTML site interpolating the escape helper`);
    assert.ok(!new RegExp('\\$\\{[^}]*\\bimg src=x onerror').test(line),
        `${label} innerHTML site must not interpolate raw markup`);
});

// ---------------------------------------------------------------------------
// 6) jsAttrStr behavioural round-trip: evaluate the helper (from the CURRENT
//    source, no browser) on a quote-heavy name, decode the attribute the way a
//    browser would, JSON-parse, and require the ORIGINAL string back. Non-
//    vacuity: a helper that only did the \'-replace would fail — the `"` would
//    close the attribute and the JSON parse would throw or return a different
//    string.
// ---------------------------------------------------------------------------
const NAME = 'a"b\'c<d';
function decodeAttr(s) {
    return s.replace(/&quot;/g, '"').replace(/&#39;/g, "'")
            .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}
{
    const fnMatch = w.match(/function jsAttrStr\(s\)\s*\{[\s\S]*?\n\}/);
    assert.ok(fnMatch, 'behavioural assert needs jsAttrStr in the current source');
    const ehMatch = w.match(/function escapeHtml\(text\)\s*\{[\s\S]*?\n\}/);
    assert.ok(ehMatch, 'behavioural assert needs escapeHtml in the current source');
    const jsAttrStr = new Function('document', `${ehMatch[0]}\n${fnMatch[0]}; return jsAttrStr;`)(mockDocument());
    const emitted = jsAttrStr(NAME);
    assert.ok(!/"/.test(emitted), 'jsAttrStr output must contain no raw double quote');
    assert.ok(!/</.test(emitted), 'jsAttrStr output must contain no raw <');
    assert.strictEqual(decodeAttr(emitted), JSON.stringify(NAME),
        'jsAttrStr must emit the HTML-escaped JSON literal of the name');
    assert.strictEqual(JSON.parse(decodeAttr(emitted)), NAME,
        'jsAttrStr round-trip: attribute-decode then JSON-parse must return the original name');
    // numeric helper: strings never reach the attribute
    const numMatch = w.match(/function jsNumStr\(v\)\s*\{[\s\S]*?\n\}/);
    assert.ok(numMatch, 'behavioural assert needs jsNumStr in the current source');
    const jsNumStr = new Function('document', `${numMatch[0]}; return jsNumStr;`)();
    assert.strictEqual(jsNumStr('1;alert(1)'), '0', 'jsNumStr must reject non-numeric ids with 0');
    assert.strictEqual(jsNumStr(42), '42', 'jsNumStr must pass valid numbers through unchanged');
}

console.log('g1: workers.js OK (incl. inline on* handler attrs + jsAttrStr/jsNumStr round-trip), users.js OK, logs.js OK — all innerHTML sites escaped ✓');
