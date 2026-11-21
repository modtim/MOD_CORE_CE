// Node-runnable static check for GSEC FX.163 p3b3b — the tenant BYOK page folded
// into the Settings page as a TAB (owner decision 10-08: hub tabs live INSIDE the
// main pages, never as new sidebar rows).
//
// Guards the FE wiring only (the child page itself is guarded by
// tests/tenant-byok.check.js):
//   * pages/settings.html ships a hidden main-tab-byok button labelled
//     "Encryption key (BYOK)", using the same class idiom as the Knowledge Base /
//     Stack TLS buttons, plus an empty hidden main-panel-byok in the panels
//     container (the markup is fetched verbatim at runtime, so it must stay EMPTY
//     here — a copy would drift from the child page),
//   * js/settings.js imports initializeTenantByokPage from ./tenant-byok.js, has a
//     main-panel-byok branch in the tab switcher calling _openByokTab(), reveals
//     main-tab-byok inside the existing _isTenantAdmin block, and loads the child
//     markup verbatim (fetch('pages/tenant-byok.html')) exactly once per mount
//     (load-once guard),
//   * the generic ?tab= restore (getActiveTab -> main-tab-<key>) therefore already
//     covers ?tab=byok — no byok-specific restore code is allowed,
//   * pages/dashboard-shell.html gained NO new sidebar link for BYOK.
//
//   node CORE/Dashboard/tests/settings-byok-tab.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'pages', 'settings.html'), 'utf8');
const js = fs.readFileSync(path.join(__dirname, '..', 'js', 'settings.js'), 'utf8');
const shell = fs.readFileSync(path.join(__dirname, '..', 'pages', 'dashboard-shell.html'), 'utf8');

// --- the tab button -------------------------------------------------------
const btn = html.match(/<button id="main-tab-byok"([^>]*)>/);
assert.ok(btn, 'main-tab-byok button is missing from settings.html');
assert.ok(/\bhidden\b/.test(btn[1]), 'main-tab-byok must ship hidden (revealed by settings.js per role)');
// The idiom is the CLASS attribute only — the other attributes legitimately differ
// (aria-controls points at each button's own panel).
const btnClassM = btn[1].match(/class="([^"]*)"/);
assert.ok(btnClassM, 'main-tab-byok button must carry a class attribute');
const btnClass = btnClassM[1];
const kbBtn = html.match(/<button id="main-tab-knowledge"([^>]*)>/);
assert.ok(kbBtn, 'main-tab-knowledge button must exist (the class idiom is compared against it)');
const tlsBtn = html.match(/<button id="main-tab-tls"([^>]*)>/);
assert.ok(tlsBtn, 'main-tab-tls button must exist (the class idiom is compared against it)');
const kbClassM = kbBtn[1].match(/class="([^"]*)"/);
assert.ok(kbClassM, 'main-tab-knowledge button must carry a class attribute');
const kbClass = kbClassM[1];
const tlsClassM = tlsBtn[1].match(/class="([^"]*)"/);
assert.ok(tlsClassM, 'main-tab-tls button must carry a class attribute');
const tlsClass = tlsClassM[1];
assert.ok(btnClass.replace(/\s*\bbymask\b/g, '').trim() === kbClass.trim(),
    'main-tab-byok must use the exact same class idiom as the Knowledge Base button');
assert.ok(btnClass.replace(/\s*\bbymask\b/g, '').trim() === tlsClass.trim(),
    'main-tab-byok must use the exact same class idiom as the Stack TLS button');
assert.ok(/Encryption key \(BYOK\)/.test(html), 'the tab label must read "Encryption key (BYOK)"');

// --- the panel ------------------------------------------------------------
const panel = html.match(/<div id="main-panel-byok"([^>]*)>([\s\S]*?)<\/div>/);
assert.ok(panel, 'main-panel-byok is missing from settings.html');
assert.ok(/\bhidden\b/.test(panel[1]), 'main-panel-byok must ship hidden');
assert.ok(panel[2].trim() === '',
    'main-panel-byok must be EMPTY in the markup — settings.js folds the child page in verbatim');

// --- settings.js: import + branch + reveal + verbatim fetch + load-once ----
// Edition-aware (FIX-CE-DASH-SWEEP-B): the CE cut denies tenant-byok.js/html,
// so manifest scrub rows remove the import (row 4330) and replace the child
// init call with a CE comment (row 4349). The FE wiring (button/panel/branch/
// fetch/load-once) stays in both trees. Detect the edition by a paid-only file.
const paidTree = fs.existsSync(path.join(__dirname, '..', 'js', 'seller.js'));
if (paidTree) {
    assert.ok(/import \{ initializeTenantByokPage \} from '\.\/tenant-byok\.js';/.test(js),
        "settings.js must import { initializeTenantByokPage } from './tenant-byok.js'");
} else {
    assert.ok(!/import \{ initializeTenantByokPage \} from '\.\/tenant-byok\.js';/.test(js),
        'CE settings.js must NOT import initializeTenantByokPage (tenant-byok.js is denied on CE)');
    assert.ok(/\/\/ CE: tenant-byok page denied \(manifest deny\) - nothing to initialize/.test(js),
        'CE settings.js must carry the scrub-row comment where the child init call was');
}
assert.ok(/panelId === 'main-panel-byok'/.test(js),
    'the tab switcher must have a main-panel-byok branch');
assert.ok(/panelId === 'main-panel-byok'[\s\S]{0,120}_openByokTab\(\)/.test(js),
    'the main-panel-byok branch must call _openByokTab()');
assert.ok(/let _byokLoaded\s*=\s*false/.test(js), 'load-once flag _byokLoaded must exist');
assert.ok(/_byokLoaded = false;/.test(js.split('export function initializeSettingsPage')[1]),
    '_byokLoaded must reset per mount (ui.js re-injects settings.html on every visit)');
if (paidTree) {
    assert.ok(/await initializeTenantByokPage\(\)/.test(js),
        'the child init must be awaited after the markup is folded in');
}

// the fetch must be verbatim: the child markup is copied in untouched, no re-authoring
const fetchStmt = js.match(/const response = await fetch\('\/pages\/tenant-byok\.html'\);[\s\S]{0,400}/);
assert.ok(fetchStmt, "settings.js must fetch('/pages/tenant-byok.html') (root-relative) verbatim into the panel");
assert.ok(/panel\.innerHTML = response\.ok \? await response\.text\(\) : ''/.test(fetchStmt[0]),
    'the child markup must be assigned verbatim (response.text()) into main-panel-byok');
assert.ok(/panel\.dataset\.byokFilled = '1'/.test(fetchStmt[0]),
    'the fill must be guarded on the panel element so a rebuilt DOM re-fills once');
assert.ok(/if \(_byokLoaded\) return;/.test(js),
    'repeat tab clicks in the same mount must not re-initialize (load-once)');

// --- the reveal: inside the existing _isTenantAdmin block ------------------
const reveal = js.match(/const _isTenantAdmin = [^;]+;[\s\S]*?if \(_isTenantAdmin\) \{([\s\S]*?)\n        \}/);
assert.ok(reveal, 'the _isTenantAdmin reveal block is missing');
assert.ok(/main-tab-byok/.test(reveal[1]),
    'main-tab-byok must be revealed inside the _isTenantAdmin block (tenant admins only)');
assert.ok(/main-tab-knowledge/.test(reveal[1]) && /main-tab-tls/.test(reveal[1]),
    'the reveal must sit next to main-tab-knowledge / main-tab-tls');

// --- ?tab=byok restore: the generic mechanism must cover it ---------------
const restore = js.match(/const wantedTab = getActiveTab\(\);[\s\S]{0,400}/);
assert.ok(restore, 'the generic ?tab= restore must still exist');
assert.ok(/main-tab-\$\{wantedTab\}/.test(restore[0]),
    'the restore is generic over main-tab-<key>, so ?tab=byok needs no extra code');
assert.ok(!/getActiveTab\(\) === 'byok'/.test(js) && !/tab=byok/.test(js),
    'no byok-specific restore hack — the generic mechanism handles ?tab=byok');

// --- no new sidebar row in the shell --------------------------------------
assert.ok(!/byok/i.test(shell),
    'dashboard-shell.html must NOT gain a BYOK sidebar link (hub tabs live inside main pages)');

// --- BYO move-out dialog (IMPL-BYO-MOVE-OUT-C-DASHBOARD, design s4) --------
// Static guards for the five dialog states + the CE wording, the structured-409
// key, the backoff poller (no tight loop), and the three new api.js routes.
const sc = fs.readFileSync(path.join(__dirname, '..', 'js', 'storage-connections.js'), 'utf8');
const api = fs.readFileSync(path.join(__dirname, '..', 'js', 'api.js'), 'utf8');

// the three routes (IMPL-A final shapes)
assert.ok(/moveOut: \(id, opts\) => fetchApi\(\s*`\/storage-connections\/\$\{id\}\/move-out`/.test(api),
    'api.js must expose moveOut on /storage-connections/{id}/move-out');
assert.ok(/moveStatus: \(id\) => fetchApi\(`\/storage-connections\/\$\{id\}\/move-status`\)/.test(api),
    'api.js must expose moveStatus on /storage-connections/{id}/move-status');
assert.ok(/moveOutCancel: \(id\) => fetchApi\(`\/storage-connections\/\$\{id\}\/move-out\/cancel`, \{ method: 'POST' \}\)/.test(api),
    'api.js must expose moveOutCancel on /storage-connections/{id}/move-out/cancel');
// the router refuses both targets at once -> the client must send exactly one
assert.ok(/target_connection_id: opts\.target_connection_id \}\s*: \{ target: 'mod_storage' \}/.test(api),
    'moveOut must send exactly one target (conn-to-conn sends only target_connection_id)');

// state 1: blocked — the CE sentence, verbatim, and the three offers
assert.ok(/const MOVE_OUT_BLOCKED_MSG =[\s\S]{0,400}Your own storage still has files in it\./.test(sc),
    'the blocked card must open with the design s4 sentence');
assert.ok(/Move them into MOD storage first\./.test(sc) &&
    /Nothing is deleted from your storage until you confirm\./.test(sc),
    'the blocked card must keep the soft-delete promise');
assert.ok(/>Move into MOD storage</.test(sc) && /Move into "\$\{escapeHtml\(c\.name\)\}"/.test(sc) &&
    />I deleted them myself</.test(sc),
    'the blocked card must offer MOD storage, another connection, and "I deleted them myself"');
// the dialog keys on the STRUCTURED 409, never the message text
assert.ok(/err\.detail\.error === 'byo_storage_not_empty'/.test(sc),
    'the deactivate catch must branch on err.detail.error, not the message');
// "I deleted them myself" re-fires the deactivate (the server re-checks; the
// client never deletes)
assert.ok(/choice\.selfDeleted === true[\s\S]{0,200}await storageConnections\.deactivate\(connId\)/.test(sc),
    'the self-deleted offer must re-fire the deactivate and let the server re-check');

// state 2: moving — progress from moved_object_count/total_objects + moved_bytes,
// backoff poller (same pattern as startMigrationProgress), no tight loop
assert.ok(/_movePoller = new BackoffPoller\(\(\) => pollMoveStatus\(connId\), \{ baseMs: 3000, maxMs: 30000 \}\)/.test(sc),
    'the move poller must reuse the BackoffPoller pattern (baseMs 3000 / maxMs 30000)');
assert.ok(!/setInterval\(/.test(sc), 'no bare setInterval anywhere (the poller owns the backoff)');
assert.ok(/Moving \$\{done\} of \$\{total\} files\$\{into\}\./.test(sc) &&
    /This can take a few minutes\. Your files keep working while they move — nothing is deleted from your storage\./.test(sc),
    'the moving card must count files, name the destination, and repeat the nothing-is-deleted promise');
assert.ok(/st\.moved_object_count \|\| 0/.test(sc) && /st\.moved_bytes \|\| 0/.test(sc) &&
    /Math\.round\(\(done \/ total\) \* 100\)/.test(sc),
    'progress comes from moved_object_count/total_objects + moved_bytes');
assert.ok(/>Cancel</.test(sc) && /moveOutCancel\(id\)/.test(sc),
    'the moving card must offer Cancel wired to the cancel route');

// state 3: moved (verified) — switch back. The foreign-files sentence is NOT shown on a
// verified move (REV-BYO-MOVE-OUT-B BLOCKING item 1: the move-status payload carries no
// foreign-file count), so the moved note is the MOVED sentence alone.
assert.ok(/All \$\{done\} files are now in MOD storage\./.test(sc) &&
    /note = `All \$\{done\} files are now in MOD storage\. Your storage is empty of MOD's files, so MOD can switch back\.`;/.test(sc) &&
    !/MOVE_OUT_MOVED_MSG\}\s*\$\{MOVE_OUT_FOREIGN_MSG\}/.test(sc),
    'the moved card must carry only the s4 moved sentence (no unconditional foreign-files sentence)');
assert.ok(/data-act="deactivate"[\s\S]{0,120}>Switch back to MOD storage</.test(sc),
    'after a verified move the card must offer "Switch back to MOD storage"');

// state 4: failed — move_error verbatim (escaped), Try again
assert.ok(/The move stopped at file \$\{done \+ 1\} of \$\{total\}\./.test(sc) &&
    /Nothing was deleted and MOD did not change which storage you use\./.test(sc),
    'the failed card must use the s4 wording');
assert.ok(/escapeHtml\(st\.move_error \|\| 'unknown error'\)/.test(sc),
    'move_error is shown escaped, never as raw innerHTML');
assert.ok(/data-act="move-retry"/.test(sc), 'the failed card must offer Try again');

// state 5: skipped_missing — the FIX-BYO-MIGRATION-SKIP-MISSING copy
assert.ok(/already missing from the old backend \(listed below\)/.test(sc) &&
    /no longer readable; the rest of your library is live\./.test(sc),
    'the skipped_missing card must reuse the FIX-BYO-MIGRATION-SKIP-MISSING copy');
assert.ok(/id="storage-move-skipped-keys"/.test(sc) &&
    /<li><code>\$\{escapeHtml\(k\)\}<\/code><\/li>/.test(sc),
    'per-object failures are listed escaped');

// wording rules: the CE copy itself carries no caps and no licence/plan wording
// (the file's own comments may name the rule; the USER-FACING strings may not).
const _ceConsts = sc.match(/const MOVE_OUT_[A-Z]+_MSG =\s*'((?:[^'\\]|\\')*)'/g);
assert.ok(_ceConsts && _ceConsts.length === 4, 'the four CE move-out strings must exist as MOVE_OUT_*_MSG consts');
const _ceCopy = _ceConsts.join(' ');
assert.ok(!/cap|licence|license|upgrade|paid|plan|edition/i.test(_ceCopy),
    'the CE move-out wording carries no quantity cap and no licence gate');

console.log('settings-byok-tab.check.js: all assertions passed (BYOK tab fold + BYO move-out dialog states)');
