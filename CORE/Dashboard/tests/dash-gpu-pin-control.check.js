// GPUCAP P1b — the Dashboard GPU CARD pin control (js/persistent-stacks.js).
// Same style as stack-keep-running.check.js / persistent-stacks-delete.check.js:
// no browser and no DB here, so every in-browser behaviour is asserted on the
// module's SOURCE TEXT, and the module is syntax-checked as an ES module.
// The control rides the EXISTING authoring path PUT /stacks/{id}/gpu-pin —
// nothing new is invented client-side, and the server's verdict always wins.
//
//   node CORE/Dashboard/tests/dash-gpu-pin-control.check.js
'use strict';
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'persistent-stacks.js'), 'utf8');

let pass = 0, fail = 0;
function check(n, name, cond) {
    if (cond) { pass++; console.log(`ok C${n} ${name}`); }
    else { fail++; console.log(`FAIL C${n} ${name}`); }
}

// A function's source: from its header to the first column-0 closing brace.
function fn(name) {
    const m = new RegExp(`(?:async )?function ${name}\\(`).exec(src);
    if (!m) return null;
    const end = src.indexOf('\n}\n', m.index);
    return end === -1 ? null : src.slice(m.index, end + 3);
}
const pinDialog = fn('_psGpuPin');
const confirmFn = fn('_psGpuPinConfirm');
const clearFn = fn('_psGpuUnpin');
const cancelFn = fn('_psGpuPinCancel');
const hostConfirm = fn('_psPinConfirm');
const hostPin = fn('_psPin');
const hostUnpin = fn('_psUnpin');
const newFns = [pinDialog, confirmFn, clearFn, cancelFn];
const newOk = newFns.every(Boolean);

// --- C1 the control exists (FAILS on the HEAD copy: it has neither) ---------
check(1, 'gpu-pin + gpu-unpin buttons and _psGpuPinConfirm exist',
    src.includes('data-ps-act="gpu-pin"') && src.includes('data-ps-act="gpu-unpin"')
    && /async function _psGpuPinConfirm\(/.test(src) && newOk);

// --- C2 saves through the EXISTING endpoint only ---------------------------
const gpuPinPaths = [...src.matchAll(/apiCall\(`([^`]*)`/g)].map((m) => m[1])
    .filter((p) => p.includes('gpu-pin'));
check(2, 'card pin PUTs /stacks/{id}/gpu-pin and no other gpu-pin route exists',
    src.includes('await apiCall(`/stacks/${id}/gpu-pin`, {')
    && gpuPinPaths.length === 2
    && gpuPinPaths.every((p) => p === '/stacks/${id}/gpu-pin')
    && /method: 'PUT'/.test(confirmFn || '') && /method: 'PUT'/.test(clearFn || ''));

// --- C3 body shape: {pin, reason} / {clear: true} --------------------------
check(3, 'confirm sends {pin, reason}, clear sends {clear: true, reason}',
    /\{\s*pin,\s*reason\s*\}/.test(confirmFn || '')
    && /\{\s*clear:\s*true,\s*reason:\s*null\s*\}/.test(clearFn || ''));

// --- C4 hidden, not disabled, for non-admins -------------------------------
const rowBtn = (src.match(/\$\{s\.pinned_worker \? `<button data-ps-act="gpu-pin"[\s\S]{0,1200}?: ''\}/) || [null])[0];
check(4, 'row buttons guarded by s.pinned_worker with an empty else, no disabled attribute, admin gate intact',
    !!rowBtn && !/\sdisabled(?![\w:])/.test(rowBtn)
    && /if \(!u \|\| u\.is_platform_admin !== true\) \{/.test(src)
    && src.includes('Persistent stacks are restricted to platform administrators.'));

// --- C5 card fetch is host-scoped -----------------------------------------
check(5, 'card fetch names only this stack\'s pinned_worker, never the fleet list',
    (pinDialog || '').includes('await apiCall(`/accelerators/worker/${encodeURIComponent(s.pinned_worker)}`)')
    && !(pinDialog || '').includes('_psWorkerNames'));

// --- C6 card id matches the server's fallback order ------------------------
check(6, 'card id is uuid || worker:kind:index (same order as the service)',
    /a\.uuid \|\| `\$\{workerName\}:\$\{kind\}:\$\{a\.index\}`/.test(pinDialog || ''));

// --- C7 offered services come from the stack's own definition --------------
check(7, 'dialog reads /definition and offers only services declaring x-mod.accelerator',
    (pinDialog || '').includes('await apiCall(`/stacks/${id}/definition`)')
    && /\['x-mod'\] \|\| \{\}\)\.accelerator/.test(pinDialog || '')
    && /if \(!acc\) continue;/.test(pinDialog || ''));

// --- C8 count floor + GpuPinIn bounds before any request -------------------
const floorIdx = (confirmFn || '').search(/if \(ids\.length < p\.count\)/);
const putIdx = (confirmFn || '').indexOf('/gpu-pin');
check(8, 'count floor and the 16 ids / 16 services bounds are enforced before the PUT',
    floorIdx > -1 && putIdx > -1 && floorIdx < putIdx
    && /ids\.length > 16/.test(confirmFn || '') && /plan\.length > 16/.test(confirmFn || ''));

// --- C9 no host-pin merge -------------------------------------------------
check(9, 'host-pin trio still targets /host-pin only and never mentions gpu-pin',
    (hostConfirm || '').includes('/stacks/${id}/host-pin')
    && (hostUnpin || '').includes('/stacks/${id}/host-pin')
    && !(hostConfirm || '').includes('gpu-pin')
    && !(hostUnpin || '').includes('gpu-pin')
    && !(hostPin || '').includes('gpu-pin')
    && (hostPin || '').includes('data-ps-act="pin-confirm"'));

// --- C10 no secret/host/IP/uuid in the new control or the test itself ------
// The forbidden literals are built by concatenation so this check's own bytes
// never contain them.
const urlLit = 'http' + ':' + '//';
const bearerLit = 'Bea' + 'rer ';
const tokLit = 'api' + '_token';
const pwLit = 'pa' + 'ssword';
const uuLit = 'u' + 'uid';
const ownBytes = fs.readFileSync(path.join(__dirname, 'dash-gpu-pin-control.check.js'), 'utf8');
const msgLines = (pinDialog || '').split('\n').concat((confirmFn || '').split('\n'), (clearFn || '').split('\n'))
    .filter((l) => l.includes('_psMsg('));
check(10, 'no url/token literal in this test, no card id or uuid interpolated into _psMsg',
    !ownBytes.includes(urlLit) && !ownBytes.includes(bearerLit)
    && !ownBytes.includes(tokLit) && !ownBytes.toLowerCase().includes(pwLit)
    && !msgLines.some((l) => new RegExp('\\$\\{[^}]*\\.' + uuLit + '\\b').test(l))
    && !msgLines.some((l) => l.includes(uuLit))
    && msgLines.length > 0);

// --- C11 CE and paid identical (no edition gate on the control) -----------
check(11, 'no isPaidEdition / _isCeEdition guard anywhere in the new control',
    newOk && newFns.every((b) => !/IsCeEdition|isPaidEdition/.test(b)));

// --- C12 ES-module syntax -------------------------------------------------
let modErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: src, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
} catch (e) { modErr = e; }
check(12, 'js/persistent-stacks.js parses as a valid ES module', !modErr);

// --- C13 fail-closed Save-pin button + confirm refuses an empty plan ------
const canSubmitRe = /const canSubmit = offered\.length > 0 && cards\.length > 0 && !loadErr;/;
const saveBtnRe = /\$\{canSubmit \? '' : ' disabled'\}>Save pin<\/button>/;
const refuseIdx = (confirmFn || '').search(/if \(!plan\.length\)/);
const confirmPutIdx = (confirmFn || '').indexOf('/gpu-pin');
check(13, 'Save pin is emitted disabled unless offered+cards+no loadErr, and confirm refuses before the PUT',
    canSubmitRe.test(pinDialog || '') && saveBtnRe.test(pinDialog || '')
    && refuseIdx > -1 && confirmPutIdx > -1 && refuseIdx < confirmPutIdx);

// --- C14 only host-allowed cards reach the checkbox list ------------------
const usableRe = /usable: !a\.admin_disabled && !!a\.schedulable && !!a\.vram_gb,/;
check(14, 'card rows carry the admin/schedulable/vram usable test, are filtered, and never usable: true',
    usableRe.test(pinDialog || '') && (pinDialog || '').includes('}).filter((c) => c.usable);')
    && !/usable: true/.test(pinDialog || ''));

console.log(`# pass ${pass} / # fail ${fail}`);
process.exit(fail === 0 ? 0 : 1);
