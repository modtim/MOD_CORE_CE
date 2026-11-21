"use client";
import { apiUrl } from "@/lib/runtime-config";
import { tryRefreshToken, AUTH_LOGOUT_EVENT } from "@/lib/api-client";

// GSEC FX.54 part 2 (owner Q37=A) — idle-warning dialog at T-2 minutes before
// idle logout, with one-click "Stay signed in" (refreshes the token) and
// "Sign out". The tier numbers come from the `session_tier` field of GET /me
// (the caller's effective tier: admin 30 min / user 60 min), matching what the
// server enforces in CORE/API/services/session_tier_enforcement.py.
//
// Same shape as the Dashboard's js/session-idle-warning.js: no new endpoint, no
// framework, in-process last-activity stamp only (the server tracks per-session
// activity itself).

const FALLBACK_IDLE_SECONDS = 3600;
const WARNING_LEAD_SECONDS = 120;
const TICK_MS = 15000;

export interface SessionTier {
  tier: string;
  idle_seconds: number | null;
  max_lifetime_seconds: number | null;
  warning_seconds_before: number;
}

let _timer: number | null = null;
let _lastActivity = Date.now();
let _tier: SessionTier | null = null;
let _open = false;

function _activity(): void {
  _lastActivity = Date.now();
}

function _overlay(): HTMLElement | null {
  if (typeof document === "undefined") return null;
  const existing = document.getElementById("session-idle-warning-modal");
  if (existing) return existing;
  const el = document.createElement("div");
  el.id = "session-idle-warning-modal";
  el.className = "fixed inset-0 z-50 bg-black/50 overflow-y-auto h-full w-full";
  el.innerHTML = `
    <div class="relative top-20 mx-auto p-5 border w-96 shadow-lg rounded-md bg-white" role="alertdialog" aria-labelledby="session-idle-warning-title">
      <h3 id="session-idle-warning-title" class="text-lg font-medium">Still there?</h3>
      <p id="session-idle-warning-message" class="text-sm mt-1"></p>
      <div class="mt-6 flex justify-end gap-2">
        <button id="session-idle-warning-signout" type="button">Sign out</button>
        <button id="session-idle-warning-stay" type="button">Stay signed in</button>
      </div>
    </div>`;
  document.body.appendChild(el);
  return el;
}

function _close(): void {
  if (typeof document === "undefined") return;
  const el = document.getElementById("session-idle-warning-modal");
  if (el) el.remove();
  _open = false;
}

async function _show(secondsLeft: number): Promise<void> {
  if (_open) return;
  _open = true;
  const el = _overlay();
  if (!el) return;
  const idle = _tier?.idle_seconds ?? FALLBACK_IDLE_SECONDS;
  const msg = el.querySelector("#session-idle-warning-message");
  if (msg) {
    msg.textContent =
      `You will be signed out in ${Math.max(0, Math.round(secondsLeft))} seconds ` +
      `(${_tier?.tier || "user"}, ${Math.round(idle / 60)} min idle).`;
  }
  const stay = el.querySelector("#session-idle-warning-stay");
  const signout = el.querySelector("#session-idle-warning-signout");
  if (stay) {
    stay.addEventListener("click", async () => {
      _close();
      _activity();
      await tryRefreshToken();  // one-click: keep the session alive
    }, { once: true });
  }
  if (signout) {
    signout.addEventListener("click", () => {
      _close();
      window.dispatchEvent(new CustomEvent(AUTH_LOGOUT_EVENT));
    }, { once: true });
  }
}

function _tick(): void {
  if (_open) return;
  const idle = _tier?.idle_seconds ?? FALLBACK_IDLE_SECONDS;
  const idleMs = idle * 1000;
  const lead = (_tier?.warning_seconds_before ?? WARNING_LEAD_SECONDS) * 1000;
  const idleFor = Date.now() - _lastActivity;
  if (idleFor >= idleMs - lead) void _show(idleMs - idleFor);
}

export async function startSessionIdleWarning(token: string | null): Promise<void> {
  if (typeof window === "undefined" || _timer !== null) return;
  if (!token) return;
  try {
    const resp = await fetch(`${apiUrl()}/me`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (resp.ok) {
      const data = await resp.json();
      _tier = (data?.session_tier as SessionTier) || null;
    }
  } catch {
    /* settings read failure server-side — fall back to the user-tier default */
  }
  window.addEventListener("pointerdown", _activity);
  window.addEventListener("keydown", _activity);
  _timer = window.setInterval(_tick, TICK_MS);
  _tick();
}

export function stopSessionIdleWarning(): void {
  if (_timer !== null) {
    window.clearInterval(_timer);
    _timer = null;
  }
  _close();
}

export function _resetSessionIdleWarning(): void {
  stopSessionIdleWarning();
  _tier = null;
  _lastActivity = Date.now();
}