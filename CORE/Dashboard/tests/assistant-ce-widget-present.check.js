// rc20-B2 check — FIX-MOD-ASSISTANT-IN-CE-UI: the Mod assistant widget must
// PAINT on CE. The shipped launcher (CORE/Dashboard/js/assistant-launcher.js)
// and panel (js/assistant-panel.js) each carried a CE hide bail inside
// `_init()` (`if (await _ceEdition()) { _initDone = true; return; }`) that
// returned before the config fetch, so on CE the topbar robot button was never
// injected and the chat panel never rendered (BUG.B46b P1, tasks.md:384; the
// shipped CE cut still read 1 for that needle).
//
// Same technique as assistant-platform-config-ce-gate.check.js: the shipped
// `_init` text is EXTRACTED VERBATIM (blockAt() brace-walk) from the shipped
// sources, so the assertions test the shipped source, not a copy.
//
//   node CORE/Dashboard/tests/assistant-ce-widget-present.check.js
//
// Asserts: no CE hide bail in either `_init`; the double-init guard, the
// attempt loop and the fail-open `_ceEdition` helper all survive; the topbar
// (top-panel) launcher button and the panel both still reach `_fetchConfig()`;
// the endpoint list is unchanged (no new fetch target).
'use strict';
const fs = require('fs');
const path = require('path');

const launcherSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'assistant-launcher.js'), 'utf8');
const panelSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'assistant-panel.js'), 'utf8');

function blockAt(src, open) {
    if (src[open] !== '{') throw new Error('not an open brace');
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (!depth) return src.slice(open, i + 1); }
    }
    throw new Error('unbalanced braces');
}
function fnSrc(src, hdr) {
    const at = src.indexOf(hdr);
    if (at < 0) throw new Error(`${hdr} not found in shipped source`);
    return `${hdr} ${blockAt(src, src.indexOf('{', at + hdr.length))}`;
}

const INIT_HDR = 'async function _init()';
const CE_HDR = 'async function _ceEdition()';
const launcherInit = fnSrc(launcherSrc, INIT_HDR);
const panelInit = fnSrc(panelSrc, INIT_HDR);
const launcherHelper = fnSrc(launcherSrc, CE_HDR);
const panelHelper = fnSrc(panelSrc, CE_HDR);

const BAIL_NEEDLE = 'if (await _ceEdition()) { _initDone = true; return; }';
const GUARD_NEEDLE = 'if (_initDone || _initInFlight) return;';
const INFLIGHT_SET = '_initInFlight = true;';
const FETCH_NEEDLE = 'await _fetchConfig()';

let failures = 0;
function check(name, cond, extra) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}

(async function () {
    // (a) the hide bail is gone from BOTH _init bodies. Old-file state (HEAD
    // copies via MCW_OVERRIDE) has the needle -> these two FAIL -> exit 1.
    check('launcher _init has no CE hide bail (widget paints on CE)', launcherInit.indexOf(BAIL_NEEDLE) < 0,
        launcherInit.indexOf(BAIL_NEEDLE) >= 0 ? 'old-file state: bail present at launcher' : '');
    check('panel _init has no CE hide bail (widget paints on CE)', panelInit.indexOf(BAIL_NEEDLE) < 0,
        panelInit.indexOf(BAIL_NEEDLE) >= 0 ? 'old-file state: bail present at panel' : '');

    // (b) fail-closed posture intact: double-init guard, in-flight set, attempt
    // loop and the config fetch all survive in each _init.
    [['launcher', launcherInit], ['panel', panelInit]].forEach(function (pair) {
        const who = pair[0];
        const body = pair[1];
        check(`${who} _init keeps the double-init guard`, body.indexOf(GUARD_NEEDLE) >= 0);
        check(`${who} _init sets _initInFlight`, body.indexOf(INFLIGHT_SET) >= 0);
        check(`${who} _init clears _initInFlight in finally`, body.indexOf('_initInFlight = false;') >= 0);
        check(`${who} _init keeps the MAX_INIT_ATTEMPTS loop`, body.indexOf('MAX_INIT_ATTEMPTS') >= 0);
        check(`${who} _init reaches _fetchConfig()`, body.indexOf(FETCH_NEEDLE) >= 0);
        check(`${who} _init never awaits _ceEdition() at all`, body.indexOf('_ceEdition()') < 0);
    });

    // (c) the fail-open carve helper STAYS defined in both files (paid tenants
    // keep every call; deleting it is out of scope).
    check('launcher still defines _ceEdition (carve helper kept)', /import\('\/js\/api\.js'\)/.test(launcherHelper) && /return false;/.test(launcherHelper));
    check('panel still defines _ceEdition (carve helper kept)', /import\('\/js\/api\.js'\)/.test(panelHelper) && /return false;/.test(panelHelper));
    check('_ceEdition call sites: only the two helper definitions remain',
        (launcherSrc.match(/_ceEdition\(\)/g) || []).length === 1 && (panelSrc.match(/_ceEdition\(\)/g) || []).length === 1);

    // (d) TOP PANEL entry: the launcher still builds + injects the topbar
    // robot button, and _init still reaches that path.
    check('launcher still builds the topbar button (aria-label survives)', /aria-label", "Open MOD Assistant"/.test(launcherSrc));
    check('launcher _init still calls _buildButton()/_mount()', launcherInit.indexOf('_buildButton()') >= 0 && launcherInit.indexOf('_mount(') >= 0);
    check('launcher _mount still inserts into the topbar (left of theme-toggle)', /themeToggle\.parentNode\.insertBefore\(buttonEl, themeToggle\)/.test(launcherSrc));
    check('launcher button id is assistant-launcher', launcherSrc.indexOf('btn.id = "assistant-launcher"') >= 0);

    // panel side: the chat panel DOM still gets appended from _init.
    check('panel _init still appends the panel DOM', panelInit.indexOf('document.body.appendChild(panelEl)') >= 0);
    check('panel _init still builds the panel', panelInit.indexOf('_buildPanel()') >= 0);

    // endpoint list byte-identical: this job adds no fetch of a new endpoint.
    const launcherEndpoints = (launcherSrc.match(/fetch\(`\$\{apiBase\}\/[a-z/]+`/g) || []).sort();
    const panelEndpoints = (panelSrc.match(/fetch\(`\$\{apiBase\}\/[a-z/]+`/g) || []).sort();
    check('launcher fetch targets are exactly /assistant/config', launcherEndpoints.length === 1 && launcherEndpoints[0].indexOf('/assistant/config') > 0);
    check('panel fetch targets are /assistant/chat + /assistant/escalate + /assistant/config',
        panelEndpoints.length === 3 && panelEndpoints.every(function (t) {
            return /\/assistant\/(chat|escalate|config)/.test(t);
        }));

    // script tags still ship (index.html:71 launcher, :74 panel).
    const indexSrc = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
    check('index.html still tags js/assistant-launcher.js', indexSrc.indexOf('js/assistant-launcher.js') >= 0);
    check('index.html still tags js/assistant-panel.js', indexSrc.indexOf('js/assistant-panel.js') >= 0);

    if (failures) { console.error(`\n${failures} check(s) FAILED`); process.exit(1); }
    console.log('\nall checks passed');
})();
