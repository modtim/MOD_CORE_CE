// Node-runnable check for the "Delete" stack-row action in js/persistent-stacks.js.
// The Dashboard has no JS unit-test runner and persistent-stacks.js is an ES
// module (imports ./api.js), so this script extracts the pieces from the source
// text and asserts them directly.
//
//   node CORE/Dashboard/tests/persistent-stacks-delete.check.js
'use strict';
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'persistent-stacks.js'), 'utf8');

let failures = 0;
function check(name, cond) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`); }
}

// --- 1. The row button: rendered ONLY for status 'created' and no host ------
// The conditional is the `${... ? `<button ...>` : ''}` inside the row's action
// cell. Grab it and evaluate it as a pure function of the stack object.
const btnExpr = src.match(
    /\$\{s\.status === 'created' && !s\.target_worker[\s\S]*?: ''\}/
);
check('delete conditional present in source', !!btnExpr);

const _psEsc = (s) => String(s == null ? '' : s).replace(
    /[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// Class-A fix (stale against rc20e SECURITY change): the row now interpolates
// the numeric-coerced `sid` (js/persistent-stacks.js: `const sid = Number(s.id)`)
// into the button, not `s.id` — so the extracted expression references `sid`,
// which the old sandbox never defined (ReferenceError). Define it exactly as the
// row function does.
const renderBtn = new Function(
    's',
    `const sid = Number(s.id); return ${btnExpr[0].replace(/^\$\{/, '').replace(/\}$/, '')};`
);

const createdNoHost = { id: 31, name: 'draft', status: 'created', target_worker: null };
const createdWithHost = { id: 32, name: 'draft', status: 'created', target_worker: 'gpu1' };
const runningNoHost = { id: 33, name: 'live', status: 'running', target_worker: null };
const stoppedWithHost = { id: 34, name: 'live', status: 'stopped', target_worker: 'gpu2' };

const shown = renderBtn(createdNoHost);
check('created + no host -> button', shown.includes('data-ps-act="delete"'));
check('button label is Delete', /">Delete<\/button>/.test(shown));
check('button carries the stack id', shown.includes('data-ps-id="31"'));
check('button is destructive-styled', shown.includes('text-destructive'));
check('no bg-blue-600 on the button', !shown.includes('bg-blue-600'));

check('created + host -> no button', renderBtn(createdWithHost) === '');
check('running + no host -> no button', renderBtn(runningNoHost) === '');
check('stopped + host -> no button', renderBtn(stoppedWithHost) === '');
// An empty-string target_worker counts as "no host" (the row renders '' there).
check('created + empty-string host -> button',
    renderBtn({ id: 35, status: 'created', target_worker: '' }).includes('data-ps-act="delete"'));

// --- 2. The action: DELETE /stacks/{id}, confirm, toast, refresh -----------
const delFn = src.match(/async function _psDelete\(id\) \{[\s\S]*?\n\}/);
check('_psDelete exists', !!delFn);
const body = delFn ? delFn[0] : '';

check('uses browser confirm (same helper as Stop/Teardown)', /confirm\(`Delete stack \$\{id\}/.test(body));
check('method is DELETE', /method: 'DELETE'/.test(body));
check('path is /stacks/{id} with no /v1', /apiCall\(`\/stacks\/\$\{id\}`/.test(body) && !body.includes('/v1'));
check('refreshes the list on success', /_psLoadStacks\(\)/.test(body));
check('success goes through _psMsg', /_psMsg\(`Stack \$\{id\} deleted\.`/.test(body));
check('failure goes through _psMsg (not ok)', /_psMsg\(`Delete failed: \$\{e\.message \|\| e\}`,\s*false\)/.test(body));
check('no innerHTML in the action', !body.includes('innerHTML'));
check('no destructive class misuse in the action', !body.includes('bg-blue-600'));

// The click handler must route the new act before the generic _psAct POST,
// otherwise a Delete button would POST /stacks/{id}/delete instead.
const route = src.match(/if \(act === 'delete'\) return _psDelete\(id\);/);
check('click handler routes act=delete to _psDelete', !!route);
check('route comes before the teardown confirm + generic _psAct',
    route && src.indexOf(route[0]) < src.indexOf("_psAct(id, act);"));

if (failures) console.error(`\n${failures} check(s) failed`);
else console.log('\nALL PASS');
process.exit(failures === 0 ? 0 : 1);
