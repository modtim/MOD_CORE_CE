// Node-runnable check for STACK-HEALTH-FINISH-1126 (F-2): the Stacks page must
// render a SEPARATE neutral `degraded: <reason>` badge driven by the worker-derived
// `status_health` + `status_health_reason` (GET stack fields; CORE/API/services/stack_observation.py
// streak, threshold setting stack_health_degrade_observations default 3). The paid
// health-grade badge (_psHealthBadge, s.health.grade) must stay untouched and
// independent. This script extracts _psEsc plus the PS-STATUS-HEALTH block and
// _psStackSig from the source text (same pattern as persistent-stacks-port-warnings-status-reason.check.js)
// and asserts: degraded renders the amber badge with the reason; healthy/absent/other
// values render nothing; a markup-looking reason is escaped; a changed status_health
// or reason changes the row signature (so the auto-refresh patcher re-renders);
// the paid badge's own function and testid are unchanged; the row renderer calls
// the new helper next to the paid one.
//
//   node CORE/Dashboard/tests/persistent-stacks-status-health-badge.check.js
//   node <this> <alt-src-path>   (mutation proof against a scratch copy)
'use strict';
const fs = require('fs');
const path = require('path');

const srcPath = process.argv[2] || path.join(__dirname, '..', 'js', 'persistent-stacks.js');
const src = fs.readFileSync(srcPath, 'utf8');

function grabBetween(startMarker, endMarker, label) {
    const a = src.indexOf(startMarker);
    const b = src.indexOf(endMarker);
    if (a < 0 || b < 0 || b <= a) {
        console.error(`FAIL could not locate the ${label} block in persistent-stacks.js`);
        process.exit(1);
    }
    return src.slice(a, b);
}

const escStart = src.indexOf('function _psEsc(s) {');
const escEnd = src.indexOf('\nfunction _psMsg');
if (escStart < 0 || escEnd < 0) {
    console.error('FAIL could not locate _psEsc in persistent-stacks.js');
    process.exit(1);
}
const escBlock = src.slice(escStart, escEnd);

const badgeBlock = grabBetween('function _psStatusHealthBadge(s) {', '// PS-STATUS-HEALTH-END', 'status-health badge');

const sigStart = src.indexOf('function _psStackSig(s) {');
const sigEnd = sigStart < 0 ? -1 : src.indexOf('\n}', sigStart);
if (sigStart < 0 || sigEnd < 0) {
    console.error('FAIL could not locate _psStackSig in persistent-stacks.js');
    process.exit(1);
}
const sigBlock = src.slice(sigStart, sigEnd + 2);

const F = new Function(escBlock + '\n' + badgeBlock + '\n' + sigBlock +
    '\nconst _psTransitLabel = (s) => null;' +
    '\nreturn { badge: _psStatusHealthBadge, sig: _psStackSig };')();

let failures = 0;
let passes = 0;
function check(name, cond) {
    if (cond) { passes++; console.log(`ok   ${name}`); }
    else { failures++; console.log(`FAIL ${name}`); }
}

const reason = 'vllm is restarting/unhealthy';

// (a) degraded + reason -> amber badge `degraded: <reason>`
const outDeg = F.badge({ status_health: 'degraded', status_health_reason: reason });
check('degraded renders badge', outDeg.includes('data-testid="ps-status-health-badge"'));
check('badge text degraded: reason', outDeg.includes(`degraded: ${reason}`));
check('badge is amber-neutral', outDeg.includes('bg-amber-100 text-amber-800'));
check('badge carries status-health attr', outDeg.includes('data-status-health="degraded"'));

// (b) degraded without a reason -> bare `degraded`
check('degraded no reason -> bare', F.badge({ status_health: 'degraded' }) ===
    '<span class="px-2 py-0.5 rounded-full text-xs font-medium bg-amber-100 text-amber-800" data-testid="ps-status-health-badge" data-status-health="degraded" title="status_health=degraded">degraded</span>');

// (c) healthy / absent / anything else -> nothing rendered
check('healthy hides badge', F.badge({ status_health: 'healthy', status_health_reason: reason }) === '');
check('absent hides badge', F.badge({}) === '' && F.badge({ status_health: null }) === '');
check('other value hides badge', F.badge({ status_health: 'pending' }) === '');

// (d) markup-looking reason is escaped
const evilR = '<img src=x onerror=alert(1)> restarting';
const outEvil = F.badge({ status_health: 'degraded', status_health_reason: evilR });
check('reason markup escaped', !outEvil.includes('<img') && outEvil.includes('&lt;img'));

// (e) changed status_health/reason changes the row signature; unchanged does not
const s1 = { id: 7, name: 'n', status: 'running', status_health: 'degraded', status_health_reason: reason };
const s2 = { id: 7, name: 'n', status: 'running', status_health: 'healthy', status_health_reason: null };
const s3 = { id: 7, name: 'n', status: 'running', status_health: 'degraded', status_health_reason: 'other is restarting/unhealthy' };
const s1b = { id: 7, name: 'n', status: 'running', status_health: 'degraded', status_health_reason: reason };
check('degraded->healthy changes sig', F.sig(s1) !== F.sig(s2));
check('changed reason changes sig', F.sig(s1) !== F.sig(s3));
check('same record same sig', F.sig(s1) === F.sig(s1b));

// (f) the paid health-grade badge is untouched and separate
check('paid badge function intact', src.includes('function _psHealthBadge(s) {') && src.includes('data-testid="ps-health-badge"'));
check('row calls both helpers', src.includes('${_psHealthBadge(s) ? \' \' + _psHealthBadge(s) : \'\'}${_psStatusHealthBadge(s) ? \' \' + _psStatusHealthBadge(s) : \'\'}'));
check('sig carries status_health keys', /sh: s\.status_health \|\| null, shr: s\.status_health_reason \|\| null,/.test(src));

console.log(`RESULT ${passes}/${failures}`);
process.exit(failures ? 1 : 0);
