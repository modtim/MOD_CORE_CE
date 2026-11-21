// GSEC FX.72 p3b (owner decision Q55=C) — "AI in this platform"
// transparency page (platform admin only).
// Backend: routers/platform_ai_inventory.py, mounted at /v1:
//   GET /platform/ai-inventory
//                         -> {features: [AiFeatureOut], models: [AiModelOut],
//                             data_flow: [{from, to, data, encrypted_in_transit}]}
// AiFeatureOut fields: key, name, purpose, model_role, data_sent[],
// stays_on_prem, enabled. AiModelOut fields: role, model_id,
// serving_backend, runs_where, available. The route is gated on
// require_platform_admin server-side (403 + audit for non-admins) — the
// nav mirrors that gate. Every value from the API is written with
// textContent (never innerHTML) so a hostile feature/model/data-flow
// field can never inject markup.
import { aiInventory, isPaidEdition } from './api.js';

function _showError(msg) {
    const box = document.getElementById('ai-error');
    if (box) { box.textContent = msg; box.classList.remove('hidden'); }
}

function _clearError() {
    const box = document.getElementById('ai-error');
    if (box) { box.textContent = ''; box.classList.add('hidden'); }
}

// 2400LR-navC — CE carve: open-core/manifest.yaml:1328 denies
// CORE/API/routers/platform_ai_inventory.py, so main.py's mount (main.py:2627)
// leaves GET /v1/platform/ai-inventory unmounted on a CE image and every call
// 404s. Same fail-open edition predicate the rest of the shell uses (api.js
// isPaidEdition, off the coalesced /platform/features entitlements.edition
// line): on CE render the "Not available in MOD Community Edition" posture the
// sibling paid panels show and NEVER fetch the denied route. The try/catch is
// belt-and-braces fail-open (isPaidEdition already swallows its own errors), so
// an unreadable payload keeps every paid call, unchanged.
async function _ceEdition() {
    try {
        return !(await isPaidEdition());
    } catch (_) {
        return false;
    }
}

// Verbatim server detail (string as-is; FastAPI 422 arrays joined as
// "field: msg" pairs — same shape as licence-review.js _errText).
function _errText(e) {
    if (!e) return '';
    const detail = e.detail !== undefined ? e.detail : e.message;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
        return detail.map(x => {
            if (!x || typeof x !== 'object') return String(x);
            const loc = Array.isArray(x.loc) ? x.loc.filter(p => p !== 'body').join('.') : '';
            return loc ? `${loc}: ${x.msg || 'invalid'}` : (x.msg || JSON.stringify(x));
        }).join('; ');
    }
    if (detail && typeof detail === 'object') {
        try { return JSON.stringify(detail); } catch { return 'Unknown error'; }
    }
    return (e && e.message) || 'Unknown error';
}

function _td(text, extraClass) {
    const td = document.createElement('td');
    td.className = extraClass ? `px-4 py-3 ${extraClass}` : 'px-4 py-3';
    td.textContent = text == null ? '—' : String(text);
    return td;
}

function _badge(text, cls) {
    const span = document.createElement('span');
    span.className = `px-2 py-0.5 rounded-full text-xs ${cls}`;
    span.textContent = text;
    return span;
}

function _badgeTd(badge) {
    const td = document.createElement('td');
    td.className = 'px-4 py-3 whitespace-nowrap';
    td.appendChild(badge);
    return td;
}

// ---------------------------------------------------------------------------
// init + load
// ---------------------------------------------------------------------------

export async function initializeAiInventoryPage() {
    const refresh = document.getElementById('ai-refresh');
    if (refresh) refresh.addEventListener('click', loadInventory);
    await loadInventory();
}

async function loadInventory() {
    _clearError();
    const featuresBody = document.getElementById('ai-features-body');
    const modelsBody = document.getElementById('ai-models-body');
    const flowList = document.getElementById('ai-dataflow-list');
    const empty = document.getElementById('ai-empty');
    if (!featuresBody || !modelsBody || !flowList) return;
    if (empty) empty.classList.add('hidden');
    featuresBody.textContent = '';
    modelsBody.textContent = '';
    flowList.textContent = '';
    // 2400LR-navC — CE: never fetch the denied route; show the same amber
    // posture ui.js's _cePaidPagePanelHtml shows for a direct #hash visit
    // (ui.js:327-:329 wording), built with DOM nodes + textContent like every
    // other value on this page. The sections stay in the markup (empty), so a
    // CE-gate regression that un-hides this child would 404 the route and the
    // net-guard catches it.
    if (await _ceEdition()) {
        const section = document.getElementById('ai-features-section');
        if (section) {
            const note = document.createElement('div');
            note.className = 'px-6 py-4 rounded-lg bg-amber-100 text-amber-900';
            const heading = document.createElement('h1');
            heading.className = 'text-xl font-semibold text-amber-900';
            heading.textContent = 'Not available in MOD Community Edition';
            const body = document.createElement('p');
            body.className = 'text-sm';
            body.textContent = "This page is not available in this edition.";
            note.appendChild(heading);
            note.appendChild(body);
            section.appendChild(note);
        }
        return;
    }
    try {
        const res = await aiInventory.get();
        const features = (res && res.features) || [];
        const models = (res && res.models) || [];
        const dataFlow = (res && res.data_flow) || [];
        if (!features.length && !models.length && !dataFlow.length) {
            if (empty) empty.classList.remove('hidden');
            return;
        }
        renderFeatures(featuresBody, features);
        renderModels(modelsBody, models);
        renderDataFlow(flowList, dataFlow);
    } catch (e) {
        featuresBody.textContent = '';
        modelsBody.textContent = '';
        flowList.textContent = '';
        _showError(_errText(e) || 'Failed to load the AI inventory');
    }
}

function renderFeatures(tbody, features) {
    features.forEach((f) => {
        const tr = document.createElement('tr');
        tr.className = 'hover:bg-gray-50';
        tr.appendChild(_td(f.name));
        tr.appendChild(_td(f.purpose));
        tr.appendChild(_td(f.model_role));
        tr.appendChild(_td(Array.isArray(f.data_sent) ? f.data_sent.join(', ') : f.data_sent));
        tr.appendChild(_badgeTd(_badge(
            f.stays_on_prem ? 'On-prem' : 'Leaves platform',
            f.stays_on_prem ? 'bg-green-100 text-green-700' : 'bg-amber-100 text-amber-700')));
        tr.appendChild(_badgeTd(_badge(
            f.enabled ? 'Enabled' : 'Disabled',
            f.enabled ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600')));
        tbody.appendChild(tr);
    });
}

function renderModels(tbody, models) {
    models.forEach((m) => {
        const tr = document.createElement('tr');
        tr.className = 'hover:bg-gray-50';
        tr.appendChild(_td(m.role));
        tr.appendChild(_td(m.model_id));
        tr.appendChild(_td(m.serving_backend));
        tr.appendChild(_td(m.runs_where));
        tr.appendChild(_td(m.available == null ? '—' : String(m.available)));
        tbody.appendChild(tr);
    });
}

function renderDataFlow(list, dataFlow) {
    dataFlow.forEach((d) => {
        const li = document.createElement('li');
        li.className = 'py-3 flex items-center justify-between gap-4';

        const left = document.createElement('div');
        left.className = 'text-gray-700';
        left.textContent = `${d.from} → ${d.to}: ${d.data}`;
        li.appendChild(left);

        li.appendChild(_badge(
            d.encrypted_in_transit ? 'Encrypted in transit' : 'Not encrypted',
            d.encrypted_in_transit ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'));

        list.appendChild(li);
    });
}
