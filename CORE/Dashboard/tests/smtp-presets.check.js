// Email (SMTP) provider presets: the preset table fills host / port / encryption,
// the Google hints carry the app-password guidance, only 'postmark' / 'custom'
// reach the API as `provider`, and the e2e-pinned ids are untouched.
//
//   node CORE/Dashboard/tests/smtp-presets.check.js

'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8');

const presetsSrc = read('js/smtp-presets.js');
const settingsSrc = read('js/settings.js');
const html = read('pages/settings.html');

(async () => {
    const mod = await import('data:text/javascript;base64,' + Buffer.from(presetsSrc).toString('base64'));
    const { SMTP_PRESETS, SMTP_PRESET_ORDER, inferSmtpPreset, smtpPresetFields, smtpProviderForSave } = mod;

    // 1) The select in settings.html lists exactly the preset keys, in order.
    const selectBody = html.match(/<select id="int-smtp-provider"[^>]*>([\s\S]*?)<\/select>/)[1];
    const optionValues = [...selectBody.matchAll(/<option value="([^"]+)"/g)].map((m) => m[1]);
    assert.deepStrictEqual(optionValues, SMTP_PRESET_ORDER, 'select options must equal SMTP_PRESET_ORDER');
    assert.deepStrictEqual(Object.keys(SMTP_PRESETS).sort(), [...SMTP_PRESET_ORDER].sort());
    for (const gone of ['ses', 'mailgun', 'sendgrid', 'brevo']) {
        assert.ok(!optionValues.includes(gone), `bulk-sender preset '${gone}' must not be a separate option`);
    }
    for (const must of ['postmark', 'custom', 'google', 'google-relay', 'outlook', 'icloud', 'fastmail', 'proton',
        'yahoo', 'zoho', 'mailbox-org', 'migadu', 'gmx', 'selfhosted', 'local']) {
        assert.ok(optionValues.includes(must), `missing preset ${must}`);
    }

    // 2) ids pinned by e2e (dash-ce-batch15.spec.ts) + the new ones.
    for (const id of ['smtp-config-form', 'int-smtp-provider', 'int-smtp-provider-hint', 'int-smtp-host', 'int-smtp-port',
        'int-smtp-user', 'int-smtp-pass', 'int-smtp-from-email', 'int-smtp-from-name', 'int-smtp-security', 'int-smtp-selfsigned']) {
        assert.ok(html.includes(`id="${id}"`), `settings.html lost #${id}`);
    }
    assert.ok(/Save SMTP/.test(html), "'Save SMTP' button text");
    assert.ok(settingsSrc.includes("'Email config saved'"), "'Email config saved' status text");
    assert.ok(settingsSrc.includes('password_set'), 'password_set flag still used');
    const secSelect = html.match(/<select id="int-smtp-security"[^>]*>([\s\S]*?)<\/select>/)[1];
    assert.deepStrictEqual([...secSelect.matchAll(/<option value="([^"]+)"/g)].map((m) => m[1]),
        ['auto', 'starttls', 'ssl', 'none']);
    assert.ok(/id="int-smtp-selfsigned"\s+type="checkbox"/.test(html));

    // 3) Preset table: host / port / encryption.
    const table = {
        postmark: ['smtp.postmarkapp.com', 587, 'auto'],
        google: ['smtp.gmail.com', 587, 'starttls'],
        'google-relay': ['smtp-relay.gmail.com', 587, 'starttls'],
        microsoft365: ['smtp.office365.com', 587, 'starttls'],
        icloud: ['smtp.mail.me.com', 587, 'starttls'],
        fastmail: ['smtp.fastmail.com', 465, 'ssl'],
        proton: ['smtp.protonmail.ch', 587, 'starttls'],
        yahoo: ['smtp.mail.yahoo.com', 465, 'ssl'],
        zoho: ['smtp.zoho.com', 465, 'ssl'],
        'mailbox-org': ['smtp.mailbox.org', 465, 'ssl'],
        migadu: ['smtp.migadu.com', 465, 'ssl'],
        gmx: ['mail.gmx.net', 587, 'starttls'],
        webde: ['smtp.web.de', 587, 'starttls'],
        selfhosted: ['', 587, 'starttls'],
        local: ['mailpit', 1025, 'none'],
    };
    for (const [key, [host, port, security]] of Object.entries(table)) {
        const f = smtpPresetFields(key);
        assert.strictEqual(f.fill, true, `${key} fills`);
        assert.strictEqual(f.host, host, `${key} host`);
        assert.strictEqual(f.port, port, `${key} port`);
        assert.strictEqual(f.security, security, `${key} security`);
    }
    assert.strictEqual(smtpPresetFields('custom').fill, false, 'custom keeps what the operator typed');
    assert.strictEqual(smtpPresetFields('local').noAuth, true);
    assert.strictEqual(smtpPresetFields('google-relay').noAuth, true);
    assert.strictEqual(smtpPresetFields('nope'), null);

    // OAuth-only: nothing filled, hint says so and points to Custom / own server.
    const outlook = smtpPresetFields('outlook');
    assert.strictEqual(outlook.fill, false);
    assert.strictEqual(outlook.oauthOnly, true);
    assert.ok(/OAuth2/.test(outlook.hint) && /Custom/.test(outlook.hint) && /own mail server/.test(outlook.hint));

    // 4) Hint text.
    const g = SMTP_PRESETS.google.hint.toLowerCase();
    for (const needle of ['smtp.gmail.com', '587', 'starttls', '465', 'full gmail / workspace address', 'app password',
        '2-step verification', 'normal google password will not work', 'admin can disable app passwords',
        'verified alias', '500', '2,000']) {
        assert.ok(g.includes(needle), `google hint must mention '${needle}'`);
    }
    const r = SMTP_PRESETS['google-relay'].hint.toLowerCase();
    for (const needle of ['smtp-relay.gmail.com', '587', 'no password', 'public ip', 'allowlisted', 'smtp relay service', 'routing']) {
        assert.ok(r.includes(needle), `relay hint must mention '${needle}'`);
    }
    assert.ok(/token/i.test(SMTP_PRESETS.proton.hint) && /custom-domain/i.test(SMTP_PRESETS.proton.hint));
    assert.ok(/app-specific/i.test(SMTP_PRESETS.icloud.hint) && /two-factor/i.test(SMTP_PRESETS.icloud.hint));
    assert.ok(/disabled by default/i.test(SMTP_PRESETS.microsoft365.hint) && /OAuth2 is not supported/i.test(SMTP_PRESETS.microsoft365.hint));
    assert.ok(/self-signed/i.test(SMTP_PRESETS.selfhosted.hint) && /2525/.test(SMTP_PRESETS.selfhosted.hint));
    assert.ok(/1025/.test(SMTP_PRESETS.local.hint));
    for (const [k, p] of Object.entries(SMTP_PRESETS)) {
        assert.ok(p.hint && p.label, `${k} has label + hint`);
        if (p.doc) assert.ok(/^https:\/\//.test(p.doc), `${k} doc url`);
    }

    // 5) Only postmark / custom reach the API as `provider`.
    assert.strictEqual(smtpProviderForSave('postmark'), 'postmark');
    for (const k of SMTP_PRESET_ORDER.filter((x) => x !== 'postmark')) {
        assert.strictEqual(smtpProviderForSave(k), 'custom', k);
    }
    assert.ok(/smtp_security: security/.test(settingsSrc) && /smtp_allow_selfsigned:/.test(settingsSrc));
    assert.ok(/provider = smtpProviderForSave\(presetKey\)/.test(settingsSrc));
    assert.ok(/provider === 'postmark' \|\| !securityEl\) \? 'auto'/.test(settingsSrc), 'postmark is saved with auto / strict');

    // 6) Saved config -> displayed preset.
    assert.strictEqual(inferSmtpPreset('smtp.gmail.com', 'custom'), 'google');
    assert.strictEqual(inferSmtpPreset('SMTP.GMAIL.COM', 'custom'), 'google');
    assert.strictEqual(inferSmtpPreset('smtp-relay.gmail.com', 'custom'), 'google-relay');
    assert.strictEqual(inferSmtpPreset('smtp.postmarkapp.com', 'custom'), 'postmark');
    assert.strictEqual(inferSmtpPreset('x', 'postmark'), 'postmark');
    assert.strictEqual(inferSmtpPreset('localhost', 'custom'), 'custom');
    assert.strictEqual(inferSmtpPreset('', undefined), 'custom');

    // 7) The real handler functions from settings.js, run against a fake DOM.
    const grab = (name) => {
        const m = settingsSrc.match(new RegExp(`function ${name}\\(\\) \\{[\\s\\S]*?\\n\\}\\n`));
        assert.ok(m, `${name} not found in settings.js`);
        return m[0];
    };
    const els = {};
    const mk = (id, extra = {}) => (els[id] = Object.assign({ id, value: '', placeholder: '', innerHTML: '', className: '', classList: {
        _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, contains(c) { return this._s.has(c); },
    } }, extra));
    ['int-smtp-provider', 'int-smtp-provider-hint', 'int-smtp-host', 'int-smtp-port', 'int-smtp-security', 'int-smtp-user',
        'int-smtp-postmark-block', 'int-smtp-creds-block'].forEach((id) => mk(id));
    const document = { getElementById: (id) => els[id] || null };
    const code = [grab('_updateSmtpProviderHint'), grab('_updateSmtpCredentialBlocks'), grab('_onSmtpProviderChange'),
        'return _onSmtpProviderChange;'].join('\n');
    const onChange = new Function('document', 'SMTP_PRESETS', 'smtpPresetFields', code)(document, SMTP_PRESETS, smtpPresetFields);

    els['int-smtp-provider'].value = 'google';
    onChange();
    assert.strictEqual(els['int-smtp-host'].value, 'smtp.gmail.com');
    assert.strictEqual(els['int-smtp-port'].value, 587);
    assert.strictEqual(els['int-smtp-security'].value, 'starttls');
    assert.ok(/app password/i.test(els['int-smtp-provider-hint'].innerHTML), 'Google hint rendered with app password');
    assert.ok(els['int-smtp-postmark-block'].classList.contains('hidden'));

    els['int-smtp-provider'].value = 'local';
    onChange();
    assert.deepStrictEqual([els['int-smtp-host'].value, els['int-smtp-port'].value, els['int-smtp-security'].value],
        ['mailpit', 1025, 'none']);

    els['int-smtp-host'].value = 'typed.example.com';
    els['int-smtp-provider'].value = 'custom';
    onChange();
    assert.strictEqual(els['int-smtp-host'].value, 'typed.example.com', 'custom does not clobber the typed host');

    els['int-smtp-provider'].value = 'outlook';
    onChange();
    assert.strictEqual(els['int-smtp-host'].value, 'typed.example.com', 'OAuth-only preset fills nothing');
    assert.ok(/OAuth2/.test(els['int-smtp-provider-hint'].innerHTML));
    assert.ok(/red/.test(els['int-smtp-provider-hint'].className));

    els['int-smtp-provider'].value = 'postmark';
    onChange();
    assert.strictEqual(els['int-smtp-host'].value, 'smtp.postmarkapp.com');
    assert.ok(!els['int-smtp-postmark-block'].classList.contains('hidden'));

    // 8) Server strings are never put through innerHTML: the test result / last
    //    error go through textContent; hints are static preset text.
    const testFn = settingsSrc.slice(settingsSrc.indexOf('async function handleTestSmtp'),
        settingsSrc.indexOf('// Storage Tab'));
    assert.ok(/status\.textContent = data\.error/.test(testFn), 'friendly test error shown via textContent');
    assert.ok(!/innerHTML/.test(testFn), 'handleTestSmtp must not use innerHTML');
    assert.ok(/lastTestedEl\.textContent = `Last tested \$\{when\} — failed\$\{err\}`/.test(settingsSrc));

    console.log('smtp-presets.check.js: all assertions passed');
})().catch((e) => { console.error(e); process.exit(1); });
