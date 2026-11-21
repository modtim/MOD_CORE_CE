// WIN.1.s16 check for CORE/Dashboard/js/downloads.js (the Downloads page).
// downloads.js is a plain script fragment: it fetches /downloads/manifest.json,
// renders one card per platform, and calls loadDownloads() at EOF. This test
// extracts the body (everything up to the trailing call), runs it in a vm
// sandbox with a stubbed fetch + document, and asserts:
//   (a) golden: manifest carries files.windows + files.windows_app -> both links
//       render (installer + MODTray app), no "not yet available" for windows;
//   (b) a manifest whose `files` dict lacks the windows key -> the Windows card
//       says "Not yet available" and carries NO href (never a dead link);
//   (c) a manifest with no `files` dict at all -> every card "Not yet available",
//       zero hrefs anywhere;
//   (d) no manifest.json (fetch throws) -> the catch block renders its honest
//       copy, zero hrefs, and points at the enrollment one-liner instead;
//   (e) the version badge only moves when manifest.version exists;
//   (f) no never-built artifact is ever advertised: no ".exe installer", no
//       "installation wizard", no ".dmg", no "Run anyway" in any render.
//
//   node --test CORE/Dashboard/tests/downloads-page.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'downloads.js'), 'utf8');
const CALL = 'loadDownloads();';
const tail = src.lastIndexOf(CALL);
assert.ok(tail > 0, 'could not locate the trailing loadDownloads() call');
const body = src.slice(0, tail);

const BANNED = ['.exe installer', 'installation wizard', 'install wizard', '.dmg',
    'Run anyway', 'More info'];

async function render(manifest) {
    const els = {};
    const mk = (id) => {
        const node = { id, innerHTML: '', textContent: '', children: [] };
        node.appendChild = (child) => node.children.push(child);
        return node;
    };
    const doc = {
        getElementById: (id) => {
            if (!els[id]) els[id] = mk(id);
            return els[id];
        },
    };
    doc.createElement = (tag) => {
        const node = { tag, className: '', innerHTML: '', children: [] };
        node.appendChild = (child) => node.children.push(child);
        return node;
    };
    const sandbox = {
        document: doc,
        console: { error: () => {} },
        fetch: () => {
            if (manifest === null) throw new Error('404 /downloads/manifest.json');
            return { json: async () => manifest };
        },
    };
    vm.runInNewContext(body, sandbox);
    await sandbox.loadDownloads();
    const container = els.platforms || mk('platforms');
    const cards = container.children.map((c) => c.innerHTML);
    const flat = cards.join('\n') + '\n' + container.innerHTML;
    return { cards, flat, version: els.version ? els.version.textContent : '' };
}

test.describe('downloads.js (WIN.1.s16)');

test('golden: windows installer + app keys both render links', async () => {
    const r = await render({ version: '1.0.76', files: {
        windows: 'MOD-Setup-1.0.76.msi', windows_app: 'MODTray-1.0.76.exe',
        macos: 'MOD-1.0.76.pkg', linux_appimage: 'MOD-1.0.76.AppImage',
        linux_deb: 'mod-1.0.76.deb' } });
    assert.strictEqual(r.cards.length, 3, 'three platform cards');
    assert.ok(r.cards[0].includes('href="/downloads/MOD-Setup-1.0.76.msi"'),
        'windows installer link present');
    assert.ok(r.cards[0].includes('href="/downloads/MODTray-1.0.76.exe"'),
        'windows app link present');
    assert.ok(r.cards[0].includes('MODTray app'), 'app link is labelled, not guessed');
    assert.strictEqual(r.version, 'v1.0.76', 'version badge follows the manifest');
    BANNED.forEach((b) => assert.ok(!r.flat.includes(b),
        `never advertises ${b}`));
});

test('missing windows key -> "Not yet available", no href', async () => {
    const r = await render({ version: '1.0.76', files: {
        macos: 'MOD-1.0.76.pkg', linux_appimage: 'MOD-1.0.76.AppImage' } });
    assert.ok(r.cards[0].includes('Not yet available'), 'windows card is honest');
    assert.ok(!r.cards[0].includes('href='), 'windows card carries no dead link');
    assert.ok(r.cards[2].includes('href="/downloads/MOD-1.0.76.AppImage"'),
        'linux still renders its own artifact');
});

test('manifest with no files dict -> nothing advertised', async () => {
    const r = await render({ version: '1.0.76' });
    assert.strictEqual(r.cards.length, 3, 'cards still render so the page is usable');
    r.cards.forEach((c, i) => {
        assert.ok(c.includes('Not yet available'), `card ${i} is honest`);
        assert.ok(!c.includes('href='), `card ${i} has no href`);
    });
});

test('no manifest.json at all -> honest catch copy, zero hrefs', async () => {
    const r = await render(null);
    const copy = r.flat;
    assert.ok(copy.includes('Downloads Not Yet Available'), 'catch copy is honest');
    assert.ok(copy.includes('enrollment one-liner'), 'points at the path that works');
    assert.ok(!copy.includes('href='), 'no link is invented');
    assert.ok(!copy.includes('build them manually'), 'no "build it yourself" over-claim');
});

test('no version in manifest -> badge untouched', async () => {
    const r = await render({ files: { windows: 'MOD-Setup-1.0.76.msi' } });
    assert.strictEqual(r.version, '', 'badge left alone');
});

test('banned wording never appears in any render', async () => {
    const manifests = [
        null,
        { version: '1.0.76' },
        { version: '1.0.76', files: { windows: 'MOD-Setup-1.0.76.msi' } },
    ];
    for (const m of manifests) {
        const r = await render(m);
        BANNED.forEach((b) => assert.ok(!r.flat.includes(b),
            `render for ${JSON.stringify(m)} must not contain ${b}`));
    }
});
