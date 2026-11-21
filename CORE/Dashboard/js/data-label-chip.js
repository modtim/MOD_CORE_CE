/**
 * GY.C7 — visible data-label markings (Dashboard half).
 *
 * Shared, CSP-clean helper: one colour map, one chip renderer, one
 * banner string. No inline handlers / no <script> — only plain HTML
 * strings the caller injects (R-167 CSP-safe). Mirrors the Workflow
 * Builder's components/data-label.tsx so both UIs mark the same labels
 * identically.
 *
 * Labels come from the same taxonomy as the server
 * (cogs.data_classification): public < internal < confidential <
 * restricted < CUI < EAR < ITAR (+ biometric, PII, PRE-RELEASE, custom).
 * public/internal are non-controlled and render subtly (or not at all
 * with subtleDefault); controlled labels render loudly.
 */

function escapeHtml(s) {
    return String(s)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
}

export function normalizeLabel(label) {
    if (label === null || label === undefined) return 'internal';
    const s = String(label).trim();
    return s || 'internal';
}

const CONTROLLED_BASE = 'font-semibold';

// Colour map — mirrors data-label.tsx.
const LABEL_STYLES = {
    public: { cls: 'border-gray-300 text-gray-400 bg-gray-100', tone: 'public', label: 'Public' },
    internal: { cls: 'border-gray-300 text-gray-500 bg-gray-100', tone: 'internal', label: 'Internal' },
    confidential: { cls: `${CONTROLLED_BASE} border-amber-500 text-amber-700 bg-amber-50`, tone: 'controlled', label: 'Confidential' },
    restricted: { cls: `${CONTROLLED_BASE} border-orange-600 text-orange-700 bg-orange-50`, tone: 'controlled', label: 'Restricted' },
    CUI: { cls: `${CONTROLLED_BASE} border-red-700 text-red-800 bg-red-50`, tone: 'export', label: 'CUI' },
    EAR: { cls: `${CONTROLLED_BASE} border-red-800 text-red-900 bg-red-100`, tone: 'export', label: 'EAR' },
    ITAR: { cls: `${CONTROLLED_BASE} border-red-900 text-red-950 bg-red-200`, tone: 'export', label: 'ITAR' },
    biometric: { cls: `${CONTROLLED_BASE} border-purple-600 text-purple-700 bg-purple-50`, tone: 'biometric', label: 'BIOMETRIC' },
    pii: { cls: `${CONTROLLED_BASE} border-purple-500 text-purple-700 bg-purple-50`, tone: 'controlled', label: 'PII' },
    'pre-release': { cls: `${CONTROLLED_BASE} border-blue-600 text-blue-700 bg-blue-50`, tone: 'controlled', label: 'PRE-RELEASE' },
};

const CUSTOM_STYLE = { cls: `${CONTROLLED_BASE} border-slate-500 text-slate-700 bg-slate-50`, tone: 'controlled', label: '' };

export function styleForLabel(label) {
    const raw = normalizeLabel(label);
    const key = raw.toUpperCase();
    if (LABEL_STYLES[key]) return LABEL_STYLES[key];
    const lc = raw.toLowerCase();
    if (LABEL_STYLES[lc]) return LABEL_STYLES[lc];
    return { ...CUSTOM_STYLE, label: raw.toUpperCase() };
}

export function isControlledLabel(label) {
    const tone = styleForLabel(label).tone;
    return tone !== 'public' && tone !== 'internal';
}

/**
 * Render the small coloured chip. Returns '' (nothing) for public when
 * subtleDefault is true. The `data-label` attribute lets node tests /
 * future selectors target it without parsing classes.
 */
export function dataLabelChip(label, { subtleDefault = true } = {}) {
    const raw = normalizeLabel(label);
    if (subtleDefault && styleForLabel(raw).tone === 'public') return '';
    const style = styleForLabel(raw);
    return `<span data-label-chip="${escapeHtml(style.label)}" ` +
        `title="Data classification: ${escapeHtml(style.label)}" ` +
        `class="inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] leading-none tracking-wide ${style.cls}">` +
        `${escapeHtml(style.label)}</span>`;
}

/**
 * CUI-banner-convention string: "CONTROLLED: <LABEL> — handle per policy".
 * Returns '' for non-controlled labels (caller renders nothing).
 */
export function controlledBannerText(label) {
    if (!isControlledLabel(label)) return '';
    return `CONTROLLED: ${styleForLabel(label).label} — handle per policy`;
}

const LEVEL_RANK = {
    PUBLIC: 0, INTERNAL: 1, CONFIDENTIAL: 2, RESTRICTED: 3,
    CUI: 4, EAR: 5, ITAR: 6, PII: 4, 'PRE-RELEASE': 4, BIOMETRIC: 4,
};

/** Pick the worst controlled label across a set. null when none. */
export function worstLabel(labels) {
    let worst = null;
    let worstRank = -1;
    for (const raw of labels || []) {
        const label = normalizeLabel(raw);
        if (!isControlledLabel(label)) continue;
        const rank = LEVEL_RANK[label.toUpperCase()] ?? 4;
        if (rank > worstRank) { worstRank = rank; worst = label; }
    }
    return worst;
}

export { escapeHtml };
