// CE overlay: Settings > AI Configuration "Easy | Advanced" view switch.
// Easy     = the "Who uses what" slot table (one row per AI slot).
// Advanced = the tabbed per-tool forms (Node Wizard, Container Adapter, ...).
// Only display is switched, through an inline style on the existing
// containers; the tab strip's own class-driven logic is never touched, and
// clearing the inline style hands visibility back to it. No external assets.
export const AI_VIEW_KEY = 'mod.ai-config.view';
export const AI_VIEW_DEFAULT = 'easy';
const BAR_ID = 'ai-view-toggle';

export function normalizeAiView(v) {
    return v === 'advanced' ? 'advanced' : AI_VIEW_DEFAULT;
}

export function readAiView(storage) {
    try { return normalizeAiView(storage && storage.getItem(AI_VIEW_KEY)); }
    catch (_e) { return AI_VIEW_DEFAULT; }
}

function _show(el, on) {
    if (el && el.style) el.style.display = on ? '' : 'none';
}

export function applyAiView(doc, view) {
    const easy = normalizeAiView(view) === 'easy';
    _show(doc.getElementById('ai-slots-table'), easy);
    _show(doc.getElementById('ai-tool-tabbar'), !easy);
    _show(doc.getElementById('ai-intro-advanced'), !easy);
    const panels = doc.querySelectorAll ? doc.querySelectorAll('.ai-tool-panel') : [];
    for (const p of panels) _show(p, !easy);
    const eb = doc.getElementById('ai-view-easy');
    const ab = doc.getElementById('ai-view-advanced');
    for (const [btn, on] of [[eb, easy], [ab, !easy]]) {
        if (!btn) continue;
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
        if (btn.classList) {
            btn.classList.toggle('bg-primary', on);
            btn.classList.toggle('text-white', on);
            btn.classList.toggle('bg-white', !on);
            btn.classList.toggle('text-gray-700', !on);
        }
    }
    const hint = doc.getElementById('ai-view-hint');
    if (hint) {
        hint.textContent = easy
            ? 'One row per AI slot: pick a stack, an endpoint or a model and save.'
            : 'Per-tool forms: Node Wizard, Container Adapter, AI Assistant, Build Repair and more.';
    }
    return easy ? 'easy' : 'advanced';
}

function _button(doc, id, label, cls) {
    const b = doc.createElement('button');
    b.id = id;
    b.type = 'button';
    b.className = cls;
    b.textContent = label;
    b.setAttribute('aria-pressed', 'false');
    return b;
}

// admin: only a platform admin sees the slot table, so everyone else keeps the
// tab forms exactly as before (no toggle, no hiding).
export function initAiConfigViewToggle(doc, storage, admin) {
    if (!admin || !doc || doc.getElementById(BAR_ID)) return null;
    const anchor = doc.getElementById('ai-intro-advanced')
        || doc.getElementById('ai-slots-table');
    if (!anchor || !anchor.parentNode) return null;
    const bar = doc.createElement('div');
    bar.id = BAR_ID;
    bar.className = 'mb-4 flex flex-wrap items-center gap-3';
    bar.setAttribute('role', 'group');
    bar.setAttribute('aria-label', 'AI configuration view');
    const base = 'px-3 py-1.5 text-sm font-medium border border-primary/40 ';
    const easy = _button(doc, 'ai-view-easy', 'Easy', base + 'rounded-l-md');
    const adv = _button(doc, 'ai-view-advanced', 'Advanced', base + 'rounded-r-md -ml-px');
    const hint = doc.createElement('span');
    hint.id = 'ai-view-hint';
    hint.className = 'text-sm text-gray-500';
    const choose = (v) => {
        const view = applyAiView(doc, v);
        try { if (storage) storage.setItem(AI_VIEW_KEY, view); } catch (_e) { /* private mode */ }
    };
    easy.addEventListener('click', () => choose('easy'));
    adv.addEventListener('click', () => choose('advanced'));
    bar.appendChild(easy);
    bar.appendChild(adv);
    bar.appendChild(hint);
    anchor.parentNode.insertBefore(bar, anchor);
    return applyAiView(doc, readAiView(storage));
}
