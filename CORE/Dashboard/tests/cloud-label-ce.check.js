// FIX-CLOUD-LABEL-CE — static guard: the Add-worker Max Uses hint says "Cloud",
// the internal DOM id keeps its name. open-core/manifest.yaml:1042 states the
// shipped posture (users see "Cloud", internal names keep "burst"); only the
// two user-visible strings lagged it. The API leg is covered by
// CORE/API/tests/test_cloud_label_ce.py; this is the Dashboard leg.
//
// Same style as apps-edge-ready-chip.check.js: failures are COLLECTED (not
// abort-on-first) so the MCW_OVERRIDE old-file proof run prints an exact F
// count; the final assert.ok is the rc gate.

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');            // CORE/Dashboard
const ROOTS = [ROOT, '/app/CORE', '/CORE', path.join(ROOT, '..')];
const JS_REL = ['js', 'workers.js'];

function pick(rel) {
    for (const r of ROOTS) {
        const p = path.join(r, ...rel);
        if (fs.existsSync(p)) return p;
    }
    return path.join(ROOT, ...rel);
}

const JS_PATH = pick(JS_REL);
const js = fs.existsSync(JS_PATH) ? fs.readFileSync(JS_PATH, 'utf8') : '';

const fails = [];
const soft = (name, cond) => { if (!cond) fails.push(name); };

// --- C0) the source was actually readable (no silent pass) -----------------
soft('C0 workers.js readable', js.length > 0);

// --- C1) the new Cloud wording is present ---------------------------------
soft('C1 hint says higher for Cloud', js.indexOf('higher for Cloud') >= 0);

// --- C2) the old burst wording is gone ------------------------------------
soft('C2 old hint higher for cloud burst absent', js.indexOf('higher for cloud burst') < 0);

// --- C3) the internal DOM id is unchanged ---------------------------------
soft('C3 add-worker-max-uses id still present', js.indexOf('add-worker-max-uses') >= 0);

assert.ok(fails.length === 0, 'cloud-label-ce: ' + fails.length + ' failure(s): ' + fails.join('; '));
