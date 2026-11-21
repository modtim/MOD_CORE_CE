// Node-runnable check for the 13.5d stack-secrets pure helpers in
// js/persistent-stacks.js (psParseSecretRef, psSecretStatus, psSecretsView,
// psApplySecretChanges + their small pure dependencies).
// The Dashboard has no JS unit-test runner, and persistent-stacks.js is an ES
// module (imports ./api.js), so this script extracts the pure block from the
// source text and asserts it directly — same pattern as
// ps-sidecar-filter.check.js.
//
//   node CORE/Dashboard/tests/ps-secrets.check.js
'use strict';
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'persistent-stacks.js'), 'utf8');

// The 13.5d pure helpers live in one contiguous block: from the PS_SECRET_REF_RE
// const down to (not including) `let _psTemplates`. It contains only pure
// functions + that one const, so it evaluates standalone.
const start = src.indexOf('const PS_SECRET_REF_RE');
const end = src.indexOf('let _psTemplates');
if (start < 0 || end < 0 || end <= start) {
    console.error('FAIL could not locate the ps secrets pure block in persistent-stacks.js');
    process.exit(1);
}
const block = src.slice(start, end);
const F = new Function(`${block}\nreturn {
    psParseSecretRef, psSecretName, psIsFileRef, psFileTarget,
    psDefaultFileTarget, psValidSecretName, psSecretStatus,
    psSecretsView, psApplySecretChanges,
};`)();

let failures = 0;
function check(name, cond, actual) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}${actual !== undefined ? ' — got: ' + JSON.stringify(actual) : ''}`); }
}

// --- psParseSecretRef --------------------------------------------------------
check('ref: whole-value ref parses', F.psParseSecretRef('${secret:DB_PASSWORD}') === 'DB_PASSWORD');
check('ref: name with dots/dashes/underscore', F.psParseSecretRef('${secret:a.b-c_1}') === 'a.b-c_1');
check('ref: surrounding whitespace trimmed', F.psParseSecretRef('  ${secret:API_KEY}  ') === 'API_KEY');
check('ref: partial interpolation is NOT a ref', F.psParseSecretRef('PRE${secret:X}') === null);
check('ref: bare name is NOT a ref', F.psParseSecretRef('DB_PASSWORD') === null);
check('ref: empty name is NOT a ref', F.psParseSecretRef('${secret:}') === null);
check('ref: leading underscore is NOT a ref', F.psParseSecretRef('${secret:_X}') === null);
check('ref: non-string is NOT a ref', F.psParseSecretRef(null) === null && F.psParseSecretRef(42) === null);
check('ref: trailing junk is NOT a ref', F.psParseSecretRef('${secret:X}y') === null
    && F.psParseSecretRef('${secret: X}') === null);

// --- psSecretStatus -----------------------------------------------------------
// API failure = known false + empty names → 'unknown', never 'missing'.
check('status: API failure reads unknown', F.psSecretStatus({ secret: 'X' }, [], false) === 'unknown');
check('status: API failure on string ref', F.psSecretStatus('X', [], false) === 'unknown');
check('status: resolved when name listed', F.psSecretStatus({ secret: 'X' }, ['X'], true) === 'resolved');
check('status: missing when known and absent', F.psSecretStatus('X', ['Y'], true) === 'missing');
check('status: plain when unnamed', F.psSecretStatus('', ['X'], true) === 'plain'
    && F.psSecretStatus({ secret: '' }, [], true) === 'plain');
check('status: file ref resolves by name', F.psSecretStatus({ type: 'file', secret: 'TLS' }, ['TLS'], true) === 'resolved');
check('status: file ref unknown on API failure', F.psSecretStatus({ type: 'file', secret: 'TLS' }, [], false) === 'unknown');

// --- psSecretsView ------------------------------------------------------------
const spec1 = {
    services: {
        web: {
            environment: {
                DB_PASSWORD: '***redacted***',            // inline value, redacted server-side
                GITHUB_TOKEN: '${secret:GITHUB_TOKEN}',   // whole-value ref
                REGION: 'us-east-1',                       // plain, stays visible
            },
        },
        api: {
            // list-form environment
            environment: ['API_KEY=${secret:API_KEY}', 'DEBUG=1'],
            'x-mod': { secrets: { cert: { type: 'file', secret: 'CERT', target: '/etc/tls/cert.pem' } } },
        },
    },
};
let view = F.psSecretsView(spec1, ['GITHUB_TOKEN', 'API_KEY', 'CERT'], true);
let web = view.services.web;
check('view: has both services', Object.keys(view.services).length === 2 && !!view.services.api);
let pw = web.envs.find((e) => e.key === 'DB_PASSWORD');
check('view: redacted value is EMPTY string', pw && pw.value === '' && pw.redacted === true);
let tok = web.envs.find((e) => e.key === 'GITHUB_TOKEN');
check('view: ref row carries name not value', tok && tok.value === '' && tok.refName === 'GITHUB_TOKEN');
check('view: ref row resolved', tok && tok.status === 'resolved');
let reg = web.envs.find((e) => e.key === 'REGION');
check('view: plain value kept', reg && reg.value === 'us-east-1' && reg.status === 'plain' && !reg.refName);
let api = view.services.api;
let akey = api.envs.find((e) => e.key === 'API_KEY');
check('view: list-form env parsed + ref detected', akey && akey.refName === 'API_KEY' && akey.value === '');
check('view: list-form plain parsed', api.envs.find((e) => e.key === 'DEBUG' && e.value === '1'));
check('view: file secret listed with target', api.files.length === 1
    && api.files[0].name === 'cert' && api.files[0].target === '/etc/tls/cert.pem'
    && api.files[0].status === 'resolved');
// The spec's raw values must NEVER appear in the rendered view data.
check('view: no raw value leaks', JSON.stringify(view).includes('***redacted***') === false
    && JSON.stringify(view).includes('PRE${secret:') === false);
// Missing tenant secret → 'missing' (known), API failure → 'unknown'.
view = F.psSecretsView(spec1, [], true);
check('view: missing status when absent', view.services.web.envs.find((e) => e.key === 'GITHUB_TOKEN').status === 'missing');
view = F.psSecretsView(spec1, [], false);
check('view: unknown status on API failure', view.services.web.envs.find((e) => e.key === 'GITHUB_TOKEN').status === 'unknown'
    && view.services.api.files[0].status === 'unknown');
check('view: null spec tolerated', Object.keys(F.psSecretsView(null, [], true).services).length === 0);

// --- psApplySecretChanges -------------------------------------------------------
const base = () => ({
    services: {
        web: { environment: { DB_PASSWORD: 'x', REGION: 'us-east-1' } },
        api: { environment: { API_KEY: 'y' } },
    },
});
// env: 'secret' form → whole-value ${secret:NAME}
let s = F.psApplySecretChanges(base(), {
    env: [
        { service: 'web', env: 'DB_PASSWORD', kind: 'secret', name: 'DB_PASSWORD' },
        { service: 'api', env: 'API_KEY', kind: 'new', name: 'api_key' },
        { service: 'web', env: 'REGION', kind: 'plain', value: 'eu-west-1' },
    ],
    files: [],
});
check('apply: env secret form', s.services.web.environment.DB_PASSWORD === '${secret:DB_PASSWORD}');
check('apply: env new form', s.services.api.environment.API_KEY === '${secret:api_key}');
check('apply: env plain form', s.services.web.environment.REGION === 'eu-west-1');
// file secret → top-level `secrets` + service list entry, compose-native form
s = F.psApplySecretChanges(base(), {
    env: [],
    files: [{ service: 'api', name: 'cert', target: '/etc/tls/cert.pem' }],
});
check('apply: file secret top-level entry', s.secrets && s.secrets.cert && s.secrets.cert.file === '/etc/tls/cert.pem');
check('apply: file secret service entry', Array.isArray(s.services.api.secrets)
    && s.services.api.secrets[0].source === 'cert' && s.services.api.secrets[0].target === '/etc/tls/cert.pem');
// default target when omitted
s = F.psApplySecretChanges(base(), { env: [], files: [{ service: 'api', name: 'TLS_KEY', target: '' }] });
check('apply: default file target', s.secrets.TLS_KEY.file === '/run/secrets/tls_key'
    && s.services.api.secrets[0].target === '/run/secrets/tls_key');
// no duplicate service entry when the name is already referenced
s = F.psApplySecretChanges(base(), {
    env: [],
    files: [{ service: 'api', name: 'cert', target: '/etc/tls/cert.pem' }],
});
s.services.api.secrets = [{ source: 'cert' }];
s = F.psApplySecretChanges(s, { env: [], files: [{ service: 'api', name: 'cert', target: '/etc/tls/cert.pem' }] });
check('apply: file entry not duplicated', s.services.api.secrets.length === 1);
// mutates + returns the same object; tolerates empty changes / nullish
s = base();
const r = F.psApplySecretChanges(s, {});
check('apply: returns spec, no-op on empty', r === s && Object.keys(s).length === 1);
check('apply: null spec returned as-is', F.psApplySecretChanges(null, {}) === null);
check('apply: unknown service skipped', (() => {
    const t = F.psApplySecretChanges(base(), { env: [{ service: 'ghost', env: 'K', kind: 'plain', value: 'v' }], files: [] });
    return !t.services.ghost;
})());

if (failures) { console.error(`\n${failures} check(s) FAILED`); process.exit(1); }
console.log('\nall checks passed');
