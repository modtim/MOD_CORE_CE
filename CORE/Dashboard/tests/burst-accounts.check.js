// Node-runnable check for the pure helpers in js/burst-accounts.js (GH.8b).
// The Dashboard has no JS unit-test runner, and burst-accounts.js is an ES
// module (imports ./api.js), so this script extracts the pure functions
// from the source text — same technique as ps-sidecar-filter.check.js —
// and asserts them directly, without touching the DOM-wiring code.
//
//   node CORE/Dashboard/tests/burst-accounts.check.js
'use strict';
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'burst-accounts.js'), 'utf8');

// Every helper closes with a `}` at column 0; nested blocks are indented,
// so a non-greedy match to the first column-0 close brace is exact.
function extract(name) {
    const m = src.match(new RegExp(`function ${name}\\([\\s\\S]*?\\n}`));
    if (!m) throw new Error(`could not extract function ${name} from burst-accounts.js`);
    return m[0];
}

// Function declarations hoist within the eval'd body, so concatenation
// order doesn't matter even though _baBuildConnectPayload/_baBuildPolicyPayload
// call _baLimitsFromFields, which calls _baParseInstanceTypes, which
// _baStatusBadge/_baBuildConnectPayload also indirectly rely on via _baEsc.
const names = [
    '_baEsc', '_baWhen', '_baCredits', '_baParseInstanceTypes',
    '_baLimitsFromFields', '_baBuildConnectPayload', '_baBuildPolicyPayload',
    '_baBuildCredential', '_baStatusBadge',
];
const body = names.map(extract).join('\n') + `\nreturn { ${names.join(', ')} };`;
const {
    _baEsc, _baWhen, _baCredits, _baParseInstanceTypes,
    _baLimitsFromFields, _baBuildConnectPayload, _baBuildPolicyPayload,
    _baBuildCredential,
    _baStatusBadge,
} = new Function(body)();

let failures = 0;
function check(name, cond, extra) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}
function deepEqual(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

// --- _baEsc ------------------------------------------------------------------
check('esc: amp/lt/gt/quotes',
    _baEsc(`<script>&"'</script>`) === '&lt;script&gt;&amp;&quot;&#39;&lt;/script&gt;');
check('esc: null/undefined -> empty string', _baEsc(null) === '' && _baEsc(undefined) === '');
check('esc: number coerced', _baEsc(42) === '42');

// --- _baWhen -------------------------------------------------------------------
check('when: null/undefined -> em dash', _baWhen(null) === '—' && _baWhen(undefined) === '—');
check('when: invalid string -> em dash', _baWhen('not-a-date') === '—');
check('when: valid iso -> non-empty, not the dash',
    _baWhen('2026-01-15T10:00:00Z') !== '—' && _baWhen('2026-01-15T10:00:00Z').length > 0);

// --- _baCredits ----------------------------------------------------------------
check('credits: null/undefined/NaN -> em dash',
    _baCredits(null) === '—' && _baCredits(undefined) === '—' && _baCredits('not-a-number') === '—');
check('credits: integer prints bare', _baCredits(5) === '5' && _baCredits(0) === '0');
check('credits: fractional gets 2dp', _baCredits(1.5) === '1.50' && _baCredits(0.005) === '0.01');
check('credits: string number coerced', _baCredits('12') === '12');

// --- _baParseInstanceTypes -------------------------------------------------------
check('parseTypes: empty/undefined/null -> []',
    deepEqual(_baParseInstanceTypes(''), []) && deepEqual(_baParseInstanceTypes(undefined), [])
    && deepEqual(_baParseInstanceTypes(null), []));
check('parseTypes: trims + drops empties',
    deepEqual(_baParseInstanceTypes('a, b ,,c'), ['a', 'b', 'c']));
check('parseTypes: single value', deepEqual(_baParseInstanceTypes('g5.xlarge'), ['g5.xlarge']));

// --- _baLimitsFromFields (exercised directly via the payload builders below,
//     but also worth a direct assertion since it is the field the API
//     replaces wholesale on PATCH) -----------------------------------------------
check('limits: blank fields omitted entirely',
    deepEqual(_baLimitsFromFields('', '', ''), {}));
check('limits: only provided fields included',
    deepEqual(_baLimitsFromFields('3', '', ''), { max_instances: 3 }));
check('limits: all three fields',
    deepEqual(
        _baLimitsFromFields('3', '40.5', 'g5.xlarge, p4d.24xlarge'),
        { max_instances: 3, max_gpu_hours_month: 40.5, allowed_instance_types: ['g5.xlarge', 'p4d.24xlarge'] },
    ));
check('limits: non-numeric max_instances omitted', deepEqual(_baLimitsFromFields('abc', '', ''), {}));

// --- _baBuildConnectPayload -----------------------------------------------------
check('connect payload: minimal (provider + secret only)',
    deepEqual(
        _baBuildConnectPayload({
            provider: 'libcloud', region: '', secretRef: 'aws-burst-prod',
            maxInstancesRaw: '', maxGpuHoursRaw: '', instanceTypesRaw: '',
        }),
        { provider: 'libcloud', secret_ref: 'aws-burst-prod' },
    ));
check('connect payload: trims provider/secretRef, omits blank region',
    deepEqual(
        _baBuildConnectPayload({
            provider: '  libcloud  ', region: '   ', secretRef: '  aws-burst-prod  ',
            maxInstancesRaw: '', maxGpuHoursRaw: '', instanceTypesRaw: '',
        }),
        { provider: 'libcloud', secret_ref: 'aws-burst-prod' },
    ));
check('connect payload: full — region + limits included',
    deepEqual(
        _baBuildConnectPayload({
            provider: 'libcloud', region: 'us-east-1', secretRef: 'aws-burst-prod',
            maxInstancesRaw: '5', maxGpuHoursRaw: '100', instanceTypesRaw: 'g5.xlarge',
        }),
        {
            provider: 'libcloud', secret_ref: 'aws-burst-prod', region: 'us-east-1',
            limits: { max_instances: 5, max_gpu_hours_month: 100, allowed_instance_types: ['g5.xlarge'] },
        },
    ));
check('connect payload: no raw credential value anywhere in the payload shape',
    !Object.keys(_baBuildConnectPayload({
        provider: 'libcloud', region: '', secretRef: 'aws-burst-prod',
        maxInstancesRaw: '', maxGpuHoursRaw: '', instanceTypesRaw: '',
    })).some((k) => /cred/i.test(k)));

// --- _baBuildPolicyPayload -------------------------------------------------------
check('policy payload: wraps limits only',
    deepEqual(_baBuildPolicyPayload('10', '200', 'a100, h100'),
        { limits: { max_instances: 10, max_gpu_hours_month: 200, allowed_instance_types: ['a100', 'h100'] } }));
check('policy payload: all blank -> empty limits object',
    deepEqual(_baBuildPolicyPayload('', '', ''), { limits: {} }));

// --- _baStatusBadge --------------------------------------------------------------
check('statusBadge: valid -> green, contains status text', /green/.test(_baStatusBadge('valid')) && _baStatusBadge('valid').includes('valid'));
check('statusBadge: invalid -> red', /red/.test(_baStatusBadge('invalid')));
check('statusBadge: pending -> amber', /amber/.test(_baStatusBadge('pending')));
check('statusBadge: unknown/missing status falls back + escapes', _baStatusBadge(undefined).includes('unknown'));
check('statusBadge: escapes status text', _baStatusBadge('<b>x</b>').includes('&lt;b&gt;'));

// --- _baBuildCredential -----------------------------------------------------
// A catalog entry shaped like the GCP one (secret + multiline SA JSON,
// required project, optional datacenter).
const GCP_FIELDS = [
    { key: 'provider_key', label: 'Service account email', secret: true, required: true, multiline: false, help: '' },
    { key: 'secret_key', label: 'Service account JSON key', secret: true, required: true, multiline: true, help: '' },
    { key: 'project', label: 'Project ID', secret: true, required: true, multiline: false, help: '' },
    { key: 'datacenter', label: 'Zone', secret: false, required: false, multiline: false, help: '' },
];
const LAMBDA_FIELDS = [
    { key: 'api_key', label: 'API key', secret: true, required: true, multiline: false, help: '' },
    { key: 'ssh_key_names', label: 'SSH key names', secret: false, required: true, multiline: false, list: true, help: '' },
    { key: 'region', label: 'Region', secret: false, required: false, multiline: false, help: '' },
];

check('credential: builds the JSON object from filled fields, trims values',
    deepEqual(
        _baBuildCredential(GCP_FIELDS, {
            provider_key: '  burst@proj.iam.gserviceaccount.com ',
            secret_key: '  {"type": "service_account"}\n',
            project: 'my-project',
            datacenter: '',
        }),
        {
            credential: {
                provider_key: 'burst@proj.iam.gserviceaccount.com',
                secret_key: '{"type": "service_account"}',
                project: 'my-project',
            },
            missing: [], anyFilled: true,
        }));

check('credential: reports missing required fields by label, in order',
    deepEqual(
        _baBuildCredential(GCP_FIELDS, { provider_key: '', secret_key: '', project: '' }).missing,
        ['Service account email', 'Service account JSON key', 'Project ID']));

check('credential: all blank -> empty credential, no values, nothing filled',
    deepEqual(
        _baBuildCredential(GCP_FIELDS, { provider_key: '   ', secret_key: '', project: '' }),
        { credential: {}, missing: ['Service account email', 'Service account JSON key', 'Project ID'], anyFilled: false }));

check('credential: optional field omitted when blank, kept when set',
    deepEqual(
        _baBuildCredential(GCP_FIELDS, {
            provider_key: 'a@b.c', secret_key: 'j', project: 'p', datacenter: 'us-central1-b',
        }).credential,
        { provider_key: 'a@b.c', secret_key: 'j', project: 'p', datacenter: 'us-central1-b' }));

check('credential: multiline value keeps internal newlines (PEM round-trip)',
    (() => {
        const pem = '-----BEGIN FAKE PRIVATE KEY-----\nline1\nline2\n-----END FAKE PRIVATE KEY-----';
        const r = _baBuildCredential(
            [{ key: 'key_content', label: 'PEM', secret: true, required: true, multiline: true, help: '' }],
            { key_content: '  ' + pem + '  ' });
        return deepEqual(r.credential.key_content, pem);
    })());

check('credential: list field splits on commas into a JSON array',
    deepEqual(
        _baBuildCredential(LAMBDA_FIELDS, {
            api_key: 'k', ssh_key_names: ' key-a , key-b ,', region: '',
        }).credential,
        { api_key: 'k', ssh_key_names: ['key-a', 'key-b'] }));

check('credential: missing values map tolerated (undefined keys = blank)',
    deepEqual(
        _baBuildCredential(LAMBDA_FIELDS, { api_key: 'k' }).credential,
        { api_key: 'k' })
    && deepEqual(
        _baBuildCredential(LAMBDA_FIELDS, { api_key: 'k' }).missing,
        ['SSH key names']));

check('credential: never mutates the input values map',
    (() => {
        const values = { provider_key: ' a ', secret_key: 'b', project: 'p' };
        const before = JSON.stringify(values);
        _baBuildCredential(GCP_FIELDS, values);
        return JSON.stringify(values) === before;
    })());

if (failures) { console.error(`\n${failures} check(s) FAILED`); process.exit(1); }
console.log('\nall checks passed');
