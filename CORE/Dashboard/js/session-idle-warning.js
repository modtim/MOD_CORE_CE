// GSEC FX.54 part 2 (owner Q37=A) — idle-warning dialog, T-2 minutes before the
// session's idle expiry, with one-click "Stay signed in" and "Sign out".
//
// The tier numbers come from the `session_tier` field of GET /me (the caller's
// effective tier: admin 30 min / user 60 min), so the dialog matches what the
// server actually enforces in services/session_tier_enforcement.py. Activity is
// any pointer/keyboard/visibility interaction; "Stay signed in" stamps activity
// and refreshes the access token through the existing proactive-refresh path.
//
// Kept deliberately small: no new endpoint, no framework, no storage beyond the
// in-process last-activity stamp (the server tracks per-session activity itself).

import { me } from './api.js';

// Sign-out callback is injected by the caller (auth.js passes handleLogout) so
// this module does not import auth.js — the cycle would make the dialog's
// handlers read an uninitialised binding on some bundlers.
let _onSignOut = null;

const WARNING_LEAD_SECONDS = 120;
const FALLBACK_IDLE_SECONDS = 3600;

let _timer = null;
let _lastActivity = Date.now();
let _tier = null;
let _open = false;

function _activity() {
    _lastActivity = Date.now();
}

function _overlay() {
    let el = document.getElementById('session-idle-warning-modal');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'session-idle-warning-modal';
    el.className = 'fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50';
    el.innerHTML = `
        <div class="relative top-20 mx-auto p-5 border w-96 shadow-lg rounded-md bg-white" role="alertdialog" aria-labelledby="session-idle-warning-title">
            <h3 id="session-idle-warning-title" class="text-lg font-medium text-gray-900">Still there?</h3>
            <p id="session-idle-warning-message" class="text-sm text-gray-600 mt-1"></p>
            <div class="mt-6 flex justify-end gap-2">
                <button id="session-idle-warning-signout" type="button" class="bg-gray-100 text-gray-700 px-4 py-2 rounded-md hover:bg-gray-200 transition-colors">Sign out</button>
                <button id="session-idle-warning-stay" type="button" class="bg-primary text-white px-4 py-2 rounded-md hover:bg-primary/90 transition-colors">Stay signed in</button>
            </div>
        </div>`;
    const container = document.getElementById('modal-container') || document.body;
    container.appendChild(el);
    return el;
}

function _close() {
    const el = document.getElementById('session-idle-warning-modal');
    if (el) el.remove();
    _open = false;
}

function _show(secondsLeft) {
    if (_open) return;
    _open = true;
    const el = _overlay();
    const msg = el.querySelector('#session-idle-warning-message');
    if (msg) {
        msg.textContent = `You will be signed out in ${Math.max(0, Math.round(secondsLeft))} seconds `
            + `(${(_tier?.tier || 'user')}, ${Math.round((_tier?.idle_seconds || FALLBACK_IDLE_SECONDS) / 60)} min idle).`;
    }
    const stay = el.querySelector('#session-idle-warning-stay');
    const signout = el.querySelector('#session-idle-warning-signout');
    if (stay) stay.addEventListener('click', () => { _close(); _activity(); }, { once: true });
    if (signout) signout.addEventListener('click', () => { _close(); if (_onSignOut) _onSignOut(); }, { once: true });
}

function _tick() {
    if (_open) return;
    const idle = (_tier?.idle_seconds) || FALLBACK_IDLE_SECONDS;
    const idleMs = idle * 1000;
    const lead = (_tier?.warning_seconds_before || WARNING_LEAD_SECONDS) * 1000;
    const idleFor = Date.now() - _lastActivity;
    if (idleFor >= idleMs - lead) _show(idleMs - idleFor);
}

export async function startSessionIdleWarning(onSignOut) {
    if (typeof window === 'undefined' || _timer) return;
    _onSignOut = typeof onSignOut === 'function' ? onSignOut : null;
    try {
        const profile = await me.get();
        _tier = profile?.session_tier || null;
    } catch (e) {
        console.warn('session-idle-warning: /me tier read failed, using fallback', e);
    }
    window.addEventListener('pointerdown', _activity);
    window.addEventListener('keydown', _activity);
    _timer = setInterval(_tick, 15000);
    _tick();
}

export function stopSessionIdleWarning() {
    if (_timer) { clearInterval(_timer); _timer = null; }
    _close();
}