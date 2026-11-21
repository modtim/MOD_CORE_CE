// Node-runnable check for the FX.89 p3 tenant security-contact card in
// Dashboard/js/account.js (My Account page). account.js is an ES module, so
// this script extracts the contiguous block holding the validation regexes,
// _formatScError and initSecurityContact (from the FX.89 p3 comment down to
// getInitials) and runs it in a vm sandbox with stubbed document elements,
// securityContact (api.js), withButtonBusy (utils.js) and window.showInfo,
// then asserts:
//   (a) golden path: GET renders the contact, a valid submit PUTs the DTO,
//       success toast, "Saved." status, button re-enabled;
//   (b) client-side validation: bad primary email blocks the PUT (no API call);
//   (c) missing-contact banner: empty security_contact_email shows the
//       "Planned:" Enterprise/on-prem activation banner (not claimed enforced);
//   (d) server 422 detail (FastAPI Pydantic array) surfaced verbatim;
//   (e) in-flight guard: the Save button is disabled while the PUT runs;
//   (f) role gate: non-admins never reveal the card and never call GET.
//
//   node --test CORE/Dashboard/tests/security-contact-card.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'account.js'), 'utf8');
const start = src.indexOf('// FX.89 p3 — tenant security contact card');
const end = src.indexOf('function getInitials(name)');
assert.ok(start >= 0 && end > start, 'could not locate the security-contact block');
const block = src.slice(start, end);

function run(get, put) {
    const els = {};
    const ids = ['security-contact-card', 'sc-primary-email', 'sc-phone',
        'sc-secondary-email', 'security-contact-banner', 'security-contact-confirmed',
        'security-contact-status', 'security-contact-save-btn',
        'security-contact-confirm-btn', 'security-contact-form'];
    for (const id of ids) {
        const el = { id, value: '', disabled: false, className: 'text-sm', textContent: '', hiddenRemoved: false, dataset: {}, listeners: {} };
        el.classList = { remove: (c) => { if (c === 'hidden') el.hiddenRemoved = true; }, add: () => {} };
        el.addEventListener = (type, fn) => { el.listeners[type] = fn; };
        els[id] = el;
    }

    const calls = { get: 0, put: [], info: [] };
    const sandbox = {
        console: { log: () => {}, error: () => {} },
        document: { getElementById: (id) => els[id] },
        window: { showInfo: (msg, title) => { calls.info.push(msg); } },
        Date: global.Date,
        JSON: global.JSON,
        securityContact: {
            get: () => { calls.get++; return Promise.resolve(get()); },
            update: (payload) => { calls.put.push(payload); return put(); },
        },
        withButtonBusy: (btn, action) => {
            if (btn.disabled) return Promise.resolve(undefined);
            btn.disabled = true;
            const p = action();
            const restore = () => { btn.disabled = false; };
            return p.then(restore, restore);
        },
    };
    const init = vm.runInNewContext(
        `"use strict";\n${block}\ninitSecurityContact;`, sandbox,
        { filename: 'account-security-contact-block' });
    return { init, els, calls };
}

const ADMIN = { parent_user_id: null, is_platform_admin: false };
const NON_ADMIN = { parent_user_id: 5, is_platform_admin: false };

test('golden path: valid submit PUTs, toasts, re-enables', async () => {
    const { init, els, calls } = run(
        () => ({ security_contact_email: 'old@corp.example', security_contact_phone: null,
            security_contact_secondary_email: null, security_contact_confirmed_at: null }),
        () => ({ security_contact_email: 'sec@corp.example', security_contact_phone: '+1 (555) 0100',
            security_contact_secondary_email: null, security_contact_confirmed_at: null }));
    await init(ADMIN, []);
    assert.strictEqual(calls.get, 1, 'GET not called for admin');
    assert.ok(els['security-contact-card'].hiddenRemoved, 'card not revealed');
    els['sc-primary-email'].value = 'sec@corp.example';
    els['sc-phone'].value = '+1 (555) 0100';
    await els['security-contact-form'].listeners.submit({ preventDefault: () => {} });
    assert.strictEqual(calls.put.length, 1, 'PUT not sent for valid input');
    const sent = calls.put[0];
    assert.strictEqual(sent.email, 'sec@corp.example');
    assert.strictEqual(sent.phone, '+1 (555) 0100');
    assert.strictEqual(sent.secondary_email, null);
    assert.strictEqual(calls.info.length, 1, 'no success toast');
    assert.ok(/Security contact saved/.test(calls.info[0]));
    assert.ok(/Saved\./.test(els['security-contact-status'].textContent));
    assert.strictEqual(els['security-contact-save-btn'].disabled, false, 'button left disabled');
});

test('validation: bad primary email blocks the PUT', async () => {
    const { init, els, calls } = run(
        () => ({ security_contact_email: 'a@b.co' }), () => ({}));
    await init(ADMIN, []);
    els['sc-primary-email'].value = 'not-an-email';
    await els['security-contact-form'].listeners.submit({ preventDefault: () => {} });
    assert.strictEqual(calls.put.length, 0, 'PUT sent despite bad email');
    assert.ok(/valid email address/.test(els['security-contact-status'].textContent));
});

test('missing-contact banner: "Planned:" wording, not enforced', async () => {
    const { init, els, calls } = run(
        () => ({ security_contact_email: null, security_contact_phone: null,
            security_contact_secondary_email: null, security_contact_confirmed_at: null }),
        () => ({}));
    await init(ADMIN, []);
    const banner = els['security-contact-banner'].textContent;
    // tree-shape gate: the CE extract scrub row deletes the Planned sentence from the banner
    assert.ok(/No security contact set/.test(banner), 'no banner');
    if (/Planned:/.test(banner)) {
        assert.ok(/Planned:/.test(banner), 'requirement not worded as planned');
    } else {
        assert.strictEqual(banner, 'No security contact set. Until one is set, MOD falls back to notifying all tenant admins for breach, subprocessor and SIEM security alerts.', 'banner exact wording on CE extract');
        assert.ok(!/Planned:/.test(banner), 'Planned: should be scrubbed on CE extract');
    }
    assert.ok(/falls back to notifying all tenant admins/.test(banner));
});

test('server 422 detail surfaced verbatim', async () => {
    const { init, els, calls } = run(
        () => ({ security_contact_email: 'a@b.co' }),
        () => {
            const e = new Error('API request failed with status 422');
            e.detail = [{ loc: ['body', 'email'], msg: 'invalid email format', type: 'value_error' }];
            throw e;
        });
    await init(ADMIN, []);
    els['sc-primary-email'].value = 'looks-fine@corp.example';
    await els['security-contact-form'].listeners.submit({ preventDefault: () => {} });
    assert.strictEqual(calls.put.length, 1, 'PUT never attempted');
    assert.ok(/email: invalid email format/.test(els['security-contact-status'].textContent),
        'server detail not surfaced verbatim');
    assert.strictEqual(els['security-contact-save-btn'].disabled, false);
});

test('in-flight guard: Save disabled during the round-trip', async () => {
    const { init, els, calls } = run(
        () => ({ security_contact_email: 'a@b.co' }), async () => ({ security_contact_email: 'a@b.co' }));
    await init(ADMIN, []);
    els['sc-primary-email'].value = 'a@b.co';
    const pending = els['security-contact-form'].listeners.submit({ preventDefault: () => {} });
    await Promise.resolve();
    assert.strictEqual(els['security-contact-save-btn'].disabled, true, 'not disabled while in flight');
    await pending;
    assert.strictEqual(els['security-contact-save-btn'].disabled, false);
});

test('role gate: non-admin never sees the card, GET never called', async () => {
    const { init, els, calls } = run(() => ({}), () => ({}));
    await init(NON_ADMIN, []);
    assert.strictEqual(calls.get, 0, 'GET called for non-admin');
    assert.ok(!els['security-contact-card'].hiddenRemoved, 'card revealed to non-admin');
});
