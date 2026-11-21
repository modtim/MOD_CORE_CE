// 2400OPS (2400LR-g5bR R3/R7): a server-sourced asset/permission name such as
// `<img src=x onerror=alert(1)>` must render as TEXT in the Bulk Permissions page,
// never as markup. Proves esc() exists in the module, is applied to the row template's
// server values, and neutralises the onerror payload.
//   node --test CORE/Dashboard/tests/bulk-permissions-xss.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const bpSrc = fs.readFileSync(path.join(root, 'js', 'bulk-permissions.js'), 'utf8');

function sliceEsc() {
    const s = bpSrc.indexOf('function esc(');
    assert.ok(s >= 0, 'esc not found in bulk-permissions.js');
    const e = bpSrc.indexOf('\n}\n', s);
    return vm.runInNewContext(bpSrc.slice(s, e + 3) + '\nesc');
}

test('an <img onerror> asset name renders as text, not markup', () => {
    const esc = sliceEsc();
    const payload = '<img src=x onerror=alert(1)>';
    const out = esc(payload);
    assert.ok(!out.includes('<') && !out.includes('>'), out);
    assert.ok(out.includes('&lt;img'), out);
    // The tag opener is neutralised, so the onerror handler can never fire.
    assert.ok(!/<img|<script/i.test(out), out);
});

test('the perm row template wraps every server value in esc()', () => {
    assert.ok(/aria-label="Select folder #\$\{esc\(perm\.target_id\)\}/.test(bpSrc),
        'target_id must be escaped in the checkbox aria-label');
    assert.ok(/data-perm-id="\$\{esc\(perm\.id\)\}"/.test(bpSrc),
        'perm.id must be escaped in the data attribute');
    assert.ok(/User #\$\{esc\(perm\.user_id \|\| perm\.user_email \|\| 'Team'\)\}/.test(bpSrc),
        'user id/email must be escaped');
    assert.ok(/Level: \$\{esc\(perm\.level\)\}/.test(bpSrc), 'level must be escaped');
    // An email like `<img src=x onerror=1>@evil` interpolated raw would open a tag.
    const email = '<img src=x onerror=1>@evil';
    assert.ok(!new RegExp("User #\\$\\{perm\\.user_id \\|\\| perm\\.user_email").test(bpSrc),
        'no bare interpolation of user data');
    void email;
});

test('result-count and per-row templates escape server data too', () => {
    for (const needle of ['esc(result.total_requested)', 'esc(result.successful)',
        'esc(result.failed)', 'esc(r.status)', 'esc(r.error_message)']) {
        assert.ok(bpSrc.includes(needle), 'missing ' + needle);
    }
    assert.ok(!/\$\{\s*r\.error_message\s*\}/.test(bpSrc), 'r.error_message must not be raw');
    assert.ok(!/\$\{\s*result\.total_requested\s*\}/.test(bpSrc), 'counts must not be raw');
});
