// 2400GX-cUI-1 (30.6.15) — the Apps panel's per-App forward-auth posture:
// the `edge_ready` chip cell + the `fail_closed` toggle (PUT /apps/{id}/auth-config,
// routers/apps.py:1255-1289; app_forward_auth.py:148-219). Static check over
// js/apps.js (same style as apps-edge-caddy-block.check.js).
//
// The 409 `edge_not_ready` reason is proven HERE only (C5): on CE the ingress ask
// token always exists (vault-init.sh:1297-1305) so edge_ready() is normally true
// and the E2E spec never asserts a 409.
//
// Failures are COLLECTED (not abort-on-first) so the old-file proof run prints an
// exact F count; the final assert.ok is the rc gate.

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');            // CORE/Dashboard
const appsJs = fs.readFileSync(path.join(ROOT, 'js', 'apps.js'), 'utf8');

const fails = [];
const soft = (name, cond) => { if (!cond) fails.push(name); };

// --- C1) helpers + the auth arm -------------------------------------------
soft('C1 _authChip', /function _authChip\(/.test(appsJs));
soft('C1 _authPosture', /function _authPosture\(/.test(appsJs));
soft('C1 _authList', /function _authList\(/.test(appsJs));
soft('C1 _authValidate', /function _authValidate\(/.test(appsJs));
soft('C1 _wireAuthSave', /function _wireAuthSave\(/.test(appsJs));
soft('C1 auth arm', /else if \(act === 'auth'\)/.test(appsJs));

// --- C2) row markup: chip cell + the auth button --------------------------
soft('C2 chip testid', /data-testid="app-auth-chip-\$\{a\.id\}"/.test(appsJs));
soft('C2 chip cell fed by row fields',
    /<td class="py-2 px-4 text-center whitespace-nowrap" data-testid="app-auth-chip-\$\{a\.id\}">\$\{_authChip\(a\.auth_config, a\.edge_ready\)\}<\/td>/.test(appsJs));
soft('C2 data-act=auth', /data-act="auth"/.test(appsJs));
soft('C2 auth button hidden when revoked', /\$\{isRevoked \? '' : `<button data-act="auth"/.test(appsJs));

// --- C3) _authChip maps true/false/undefined to the three word pairs ------
const _chipStart = appsJs.indexOf('function _authChip(');
const chipFn = _chipStart < 0 ? '' : appsJs.slice(_chipStart, _chipStart + 1200);
soft('C3 posture pair', /'fail-closed' : 'fail-open'/.test(chipFn));
soft('C3 unknown not undefined', /'unknown' : \(fc \? 'fail-closed' : 'fail-open'\)/.test(chipFn));
soft('C3 edge can verify', /'edge can verify'/.test(chipFn));
soft('C3 edge cannot verify', /'edge cannot verify'/.test(chipFn));
soft('C3 chip names no token/Vault', !/ask_token|vault|VAULT/.test(chipFn));

// --- C4) PUT body keys exact + tri-state fail_closed ----------------------
const _saveStart = appsJs.indexOf('function _wireAuthSave(');
const saveFn = _saveStart < 0 ? '' : appsJs.slice(_saveStart, _saveStart + 2400);
soft('C4 PUT auth-config',
    /await apiCall\(`\/apps\/\$\{appId\}\/auth-config`, \{[\s\S]{0,80}method: 'PUT', body: JSON\.stringify\(body\)/.test(saveFn));
soft('C4 body keys',
    /const body = \{[\s\S]*require_login: requireLogin,[\s\S]*allowed_groups: groups,[\s\S]*public_paths: paths,/.test(saveFn));
soft('C4 tri-state fail_closed',
    /fcSel === 'on' \? \{ fail_closed: true \}\s*: fcSel === 'off' \? \{ fail_closed: false \} : \{\}/.test(saveFn));
soft('C4 fail_closed never null', !/fail_closed: (undefined|null)/.test(saveFn));

// --- C5) 409 branch keys on status + detail, never .code -------------------
soft('C5 status === 409', /status === 409/.test(saveFn));
soft('C5 renders err.detail', /err\.detail \|\| err\.message/.test(saveFn));
soft('C5 fix hint', /Provision the ingress ask token \(vault-init\.sh:1297-1305\) or leave fail_closed off\./.test(saveFn));
soft('C5 no .code read', !/\.code\b/.test(saveFn));

// --- C6) no console./localStorage in the added region ----------------------
const _regionEnd = appsJs.indexOf('function _fmtCreated(');
const _authRegion = _chipStart >= 0 && _regionEnd > _chipStart
    ? appsJs.slice(_chipStart, _regionEnd) : '';
soft('C6 no console.', !/console\./.test(_authRegion));
soft('C6 no localStorage', !/localStorage/.test(_authRegion));
soft('C6 no raw server value in innerHTML',
    !/innerHTML\s*=\s*[^`]*\$\{[^}]*\b(cfg|detail|groups|paths)\.[^}]*\}/.test(_authRegion));

// --- C7) still valid JS (the cdu-AR `--check` method, n3ib brief :30) ------
const tmp = path.join(os.tmpdir(), `cui1-apps-syntax-${process.pid}.mjs`);
fs.writeFileSync(tmp, appsJs);
let jsErr = null;
try {
    execFileSync(process.execPath, ['--check', tmp], { stdio: ['pipe', 'pipe', 'pipe'] });
} catch (e) {
    jsErr = String(e && (e.stderr || e.message));
} finally {
    fs.unlinkSync(tmp);
}
soft('C7 syntax', !jsErr);

if (fails.length) {
    console.log(`FAILED (${fails.length}): ${fails.join(', ')}`);
    if (jsErr) console.log(`syntax: ${(jsErr || '').split('\n')[0] || 'unknown'}`);
}
assert.ok(fails.length === 0,
    `apps-auth-config-chip.check.js: ${fails.length} failed — ${fails.join('; ') || 'none'}`);
console.log(`apps-auth-config-chip.check.js: ${27 - fails.length}/27 assertions — All assertions passed`);
