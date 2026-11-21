// logs-llm-panel.js
// Phase 5 — wires LLM panels in the logs/details views to the
// /support/logs-chat endpoint.
//
// Two host surfaces use this module:
//   1. Task Logs modal (#ai-chat-panel inside #task-logs-modal) —
//      single-task log diagnosis.
//   2. Workflow Run Details modal (#workflow-llm-panel inside
//      #workflow-detail-modal) — whole-run failure diagnosis.
//
// Each surface owns a separate panel state (selectors, current
// taskId/session, log_tail getter) so the two can co-exist without
// stomping on each other's wiring.

import { apiCall } from './api.js';

const _DEFAULTS = {
    panelSelector: '#ai-chat-panel',
    messagesSelector: '#ai-chat-messages',
    formSelector: '#ai-chat-form',
    inputSelector: '#ai-chat-input',
    placeholderHTML:
        '<p class="text-xs"><strong>Ask me about this task\'s log.</strong></p>' +
        '<p class="text-xs text-gray-400 mt-1">' +
        "E.g., \"Why did this fail?\" or \"What should I try next?\"</p>",
    logTailGetter: () => {
        const el = document.getElementById('task-log-content');
        return el ? (el.textContent || '') : '';
    },
};

// state keyed by panelSelector, so the task-logs-modal and the
// workflow-detail-modal each maintain independent sessions.
const _state = new Map();

function _getState(opts) {
    const key = opts.panelSelector;
    if (!_state.has(key)) {
        _state.set(key, {
            opts,
            session: null,
            currentTaskId: null,
            wired: false,
            submitHandler: null,
        });
    } else {
        // refresh opts in case selectors / getter changed across calls
        _state.get(key).opts = opts;
    }
    return _state.get(key);
}

function _appendMessage(messagesEl, role, text, sources) {
    if (!messagesEl) return;
    const placeholder = messagesEl.querySelector('.logs-llm-placeholder');
    if (placeholder) placeholder.remove();
    const intro = messagesEl.querySelector('.text-center.text-gray-500');
    if (intro) intro.remove();

    const el = document.createElement('div');
    el.className = role === 'user'
        ? 'rounded-lg bg-primary/10 border border-primary px-3 py-2 text-sm text-primary'
        : 'rounded-lg bg-white border border-gray-200 px-3 py-2 text-sm text-gray-800 shadow-sm';
    const who = document.createElement('div');
    who.className = 'text-xs font-semibold text-gray-500 mb-1';
    who.textContent = role === 'user' ? 'You' : 'Assistant';
    const body = document.createElement('div');
    body.style.whiteSpace = 'pre-wrap';
    body.textContent = text;
    el.appendChild(who);
    el.appendChild(body);
    if (Array.isArray(sources) && sources.length) {
        const ul = document.createElement('ul');
        ul.className = 'mt-2 text-xs text-primary space-y-0.5';
        sources.forEach((s) => {
            const li = document.createElement('li');
            const a = document.createElement('a');
            a.href = s.url || '#';
            a.target = '_blank';
            a.rel = 'noopener noreferrer';
            a.className = 'underline';
            a.textContent = s.title || s.url || 'doc';
            li.appendChild(a);
            ul.appendChild(li);
        });
        el.appendChild(ul);
    }
    messagesEl.appendChild(el);
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

function _setBusy(formEl, inputEl, busy) {
    if (!inputEl || !formEl) return;
    const btn = formEl.querySelector('button[type="submit"]');
    inputEl.disabled = busy;
    if (btn) btn.disabled = busy;
}

async function _sendTurn(state, message) {
    const { opts } = state;
    const taskId = state.currentTaskId;
    if (!taskId) return;

    const messagesEl = document.querySelector(opts.messagesSelector);
    const formEl = document.querySelector(opts.formSelector);
    const inputEl = document.querySelector(opts.inputSelector);
    if (!messagesEl || !formEl || !inputEl) return;

    const fullLog = opts.logTailGetter ? (opts.logTailGetter() || '') : '';
    // Send only the last 8 KB on the first turn; subsequent turns
    // re-use the server-side pinned copy.
    const log_tail = state.session ? null : fullLog.slice(-8192);

    const body = {
        message,
        task_id: String(taskId),
        session_id: state.session ? state.session.id : null,
        log_tail,
    };

    _setBusy(formEl, inputEl, true);
    _appendMessage(messagesEl, 'user', message);
    let typing = null;
    try {
        typing = document.createElement('div');
        typing.className = 'text-xs italic text-gray-500';
        typing.textContent = 'Assistant is thinking…';
        messagesEl.appendChild(typing);
        messagesEl.scrollTop = messagesEl.scrollHeight;

        const resp = await apiCall('/support/logs-chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (typing && typing.parentNode) typing.remove();

        if (!resp || !resp.response) {
            _appendMessage(messagesEl, 'assistant',
                "I couldn't reach the support service. Try again in a moment.");
            return;
        }
        state.session = { id: resp.session_id, taskId };
        _appendMessage(messagesEl, 'assistant', resp.response, resp.sources || []);
    } catch (e) {
        if (typing && typing.parentNode) typing.remove();
        console.error('logs-llm-panel: send failed', e);
        _appendMessage(messagesEl, 'assistant',
            'Sorry — the assistant is unreachable right now. ' +
            'Try the floating support chat in the bottom-right or contact support.');
    } finally {
        _setBusy(formEl, inputEl, false);
    }
}

export function initLogsLLMPanel(taskId, userOpts = {}) {
    const opts = { ..._DEFAULTS, ...userOpts };
    const panel = document.querySelector(opts.panelSelector);
    const messages = document.querySelector(opts.messagesSelector);
    const form = document.querySelector(opts.formSelector);
    const input = document.querySelector(opts.inputSelector);
    if (!panel || !messages || !form || !input) return;

    const state = _getState(opts);

    if (taskId !== state.currentTaskId) {
        state.session = null;
        messages.innerHTML = '';
        const intro = document.createElement('div');
        intro.className = 'logs-llm-placeholder text-center text-gray-500 text-sm py-4';
        intro.innerHTML = opts.placeholderHTML;
        messages.appendChild(intro);
    }
    state.currentTaskId = taskId;

    panel.classList.remove('hidden');
    // Claim the (possibly shared) form. The legacy ai-chat.js handler
    // checks this mode and stands down, so only this panel responds.
    form.dataset.mode = 'logs-llm';

    if (state.wired) return;
    state.wired = true;
    state.submitHandler = (e) => {
        e.preventDefault();
        // If another panel has since reclaimed this shared form, stand down.
        if (form.dataset.mode && form.dataset.mode !== 'logs-llm') return;
        const text = (input.value || '').trim();
        if (!text) return;
        input.value = '';
        _sendTurn(state, text);
    };
    form.addEventListener('submit', state.submitHandler);
}

export function resetLogsLLMPanel(userOpts = {}) {
    const opts = { ..._DEFAULTS, ...userOpts };
    const state = _state.get(opts.panelSelector);
    if (!state) return;
    state.session = null;
    state.currentTaskId = null;
    const messages = document.querySelector(opts.messagesSelector);
    if (messages) messages.innerHTML = '';
}
