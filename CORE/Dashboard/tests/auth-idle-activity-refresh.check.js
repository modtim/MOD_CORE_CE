//  (F-1 of
// , commit 6d0321074a).
//
// js/idle-warning.js refreshes the session on user activity (within the 90 s
// margin) only when the caller passes `onActivityRefresh`. The Dashboard's
// startIdleWarningDialog() in js/auth.js must pass the same refreshAccessToken
// that the proactive-refresh path uses, otherwise the activity-triggered refresh
// is inert. Static-text check: auth.js is a browser module with sibling imports,
// so the source is read and the start call is inspected (no DOM needed).
//
//   node --test CORE/Dashboard/tests/auth-idle-activity-refresh.check.js
import assert from 'node:assert';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const authSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'auth.js'), 'utf8');

// The body of startIdleWarningDialog(): from its header up to rearmIdleWarning().
function startIdleWarningDialogBody(src) {
    const header = 'function startIdleWarningDialog() {';
    const start = src.indexOf(header);
    assert.ok(start >= 0, 'startIdleWarningDialog() not found in auth.js');
    const end = src.indexOf('function rearmIdleWarning() {', start);
    assert.ok(end > start, 'rearmIdleWarning() not found after startIdleWarningDialog()');
    return src.slice(start, end);
}

test('startIdleWarningDialog passes onActivityRefresh: refreshAccessToken to startIdleWarning', () => {
    const body = startIdleWarningDialogBody(authSrc);
    assert.match(body, /startIdleWarning\(\{/, 'start call to startIdleWarning missing');
    assert.match(body, /onActivityRefresh:\s*refreshAccessToken\s*,/,
        'onActivityRefresh must be passed as refreshAccessToken (the same refresh the interval uses)');
    // The existing onStay wiring must stay exactly as it was.
    assert.match(body, /onStay:\s*refreshAccessToken\s*,/, 'onStay must keep refreshAccessToken');
});
