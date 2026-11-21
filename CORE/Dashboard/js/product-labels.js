/**
 * product-labels.js — Oz theming RUNTIME label hook (OZ-WIRING, 11-14).
 *
 * ONE hook every other OZ leg keys off. A surface opts in with ONE attribute
 * on the element whose text is the product name (sidebar <a>, page <h1>,
 * sub-tab label):
 *
 *   data-theme-key="<map key>"   (+ data-today-label="<literal>" ONLY when
 *                                  the element's text at first paint is not
 *                                  already today's literal, e.g. a sub-tab
 *                                  label a page script wrote)
 *
 * Nothing else changes: ids, hrefs, routes, page files and manifest rows
 * never move (labels change, ids never).
 *
 * Renders themeLabel(key, today, flag) from js/product-names.js (OZ-A3):
 *  - flag OFF (MOD_THEME_NAMES=0|off|false|no) => a NO-OP: the shipped
 *    literal stays byte for byte, so the OZ-A3 guard
 *    (open-core/tests/test_mod_theme_names_off_equals_today_literals.py)
 *    stays green;
 *  - flag ON => the map `label`; an unknown key is left untouched (never
 *    blank, never an error);
 *  - `paidOnly` rows (Rainbow MOD Cloud) never render in CE: the element
 *    keeps its literal. Fail-closed like ui.js _isCeEdition — an
 *    unreadable entitlement payload counts as CE.
 *
 * Runs on DOMContentLoaded (main.js mounts the sidebar shell there) and on
 * every `mod:page-rendered` event ui.js dispatches after it sets #page-content
 * innerHTML — inline <script> inside innerHTML never executes (ui.js:1050),
 * so the applier must live in a module loaded once, not in a page file.
 */
import { PRODUCT_NAMES, themeLabel, themeEnabled } from './product-names.js';
import { isPaidEdition } from './api.js';

export function applyThemeLabels(root, flag, isCe) {
    if (!themeEnabled(flag)) return 0; // OFF: byte-identical no-op
    let changed = 0;
    for (const el of root.querySelectorAll('[data-theme-key]')) {
        const key = el.dataset.themeKey;
        const entry = PRODUCT_NAMES[key];
        if (!entry || !entry.label) continue; // unknown key: untouched
        if (entry.paidOnly && isCe) continue; // paid-only surface never shows in CE
        const today = el.dataset.todayLabel ?? el.textContent;
        const want = themeLabel(key, today, flag);
        if (want !== el.textContent) { el.textContent = want; changed++; }
    }
    return changed;
}

// OZ tab titles: theme-on only; the <title> element cannot hold a span, so the
// themed name is written to document.title by script. Key = the first h1's
// data-theme-key, else ROUTE_TITLE_KEYS by the plain title text. Flag OFF = 0.
const ROUTE_TITLE_KEYS = { 'compliance': 'compliance-center' };
const TITLE_SUFFIX = ' — MOD';

export function applyThemeTitle(doc, flag, isCe) {
    if (!themeEnabled(flag)) return 0; // OFF: document.title stays byte for byte
    if (!doc || typeof doc.querySelector !== 'function') return 0;
    const cur = doc.title || '';
    if (!cur.endsWith(TITLE_SUFFIX)) return 0; // only our own ' — MOD' titles
    const plain = cur.slice(0, -TITLE_SUFFIX.length);
    if (!plain) return 0; // never blank a title
    const h1Key = doc.querySelector('h1 [data-theme-key], h1[data-theme-key]');
    const key = h1Key ? h1Key.dataset.themeKey : ROUTE_TITLE_KEYS[plain.toLowerCase()];
    const entry = key ? PRODUCT_NAMES[key] : undefined;
    if (!entry || !entry.label) return 0; // unknown key: untouched
    if (entry.paidOnly && isCe) return 0; // paid-only surface never shows in CE
    const want = themeLabel(key, plain, flag) + TITLE_SUFFIX;
    if (want === cur) return 0;
    doc.title = want;
    return 1;
}

// Edition stamp, fail-closed: unknown counts as CE until the entitlements
// payload says otherwise (same rule as ui.js _isCeEdition).
let _ozIsCe = true;

async function _wireOzLabelHook() {
    try { _ozIsCe = !(await isPaidEdition()); } catch (_ozErr) { _ozIsCe = true; }
    applyThemeLabels(document, window.MOD_THEME_NAMES, _ozIsCe);
    applyThemeTitle(document, window.MOD_THEME_NAMES, _ozIsCe); // OZ tab titles
}

// Registered at module scope (synchronously): a page nav can never race past
// the hook, and the listener stays live even if the edition fetch fails.
document.addEventListener('mod:page-rendered', () => {
    applyThemeLabels(document, window.MOD_THEME_NAMES, _ozIsCe);
    applyThemeTitle(document, window.MOD_THEME_NAMES, _ozIsCe); // OZ tab titles
});
document.addEventListener('DOMContentLoaded', _wireOzLabelHook);
