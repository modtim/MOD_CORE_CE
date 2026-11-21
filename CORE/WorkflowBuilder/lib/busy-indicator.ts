// FIX-WB-CLICKED-CONTROL-BUSY-STATE (11-17)
// Owner Tim, verbatim: "the button should go triggered until the thing is
// done loading. That way it looks clicked". Port of the Dashboard's
// js/busy-indicator.js to the Workflow Builder. The control the user just
// CLICKED shows a pressed/busy state until the requests it started finish.
//   1. a capture-phase click listener remembers the last clicked interactive
//      element (button, a[href], [role=button], [role=tab], input[type=submit])
//      and the time (Enter/Space keydown on a focused control records too);
//   2. a request started through the builder's API layer
//      (authenticatedFetch) within CLICK_ATTACH_WINDOW_MS after that click
//      attaches to that element: while at least one attached request is in
//      flight the element has class `mod-busy`, aria-busy="true", a CSS-only
//      spinner and a pressed look, and further clicks on it are swallowed;
//   3. no request within the window: nothing is shown;
//   4. stream / notification / poll endpoints never attach;
//   5. hard 60 s safety release; release always runs in `finally`;
//   6. prefers-reduced-motion: no spinner animation, pressed look kept.
// Self-contained: injects its own tiny CSS, no theme dependency. Shared-code
// UX addition (shows a pressed/busy state on the clicked control only).

export const CLICK_ATTACH_WINDOW_MS = 150;
export const BUSY_SAFETY_MS = 60000;

// Endpoints that are streams, tickets for streams or periodic polls.
const EXCLUDED_URL = /\/(notifications|stream|stream-tickets|events|poll|heartbeat|health)(\/|\?|$)/i;

interface BusyRecord {
  count: number;
  startedAt: number;
  spinner: HTMLElement;
  timer: ReturnType<typeof setTimeout> | null;
}

export interface BusyToken {
  el: HTMLElement;
  rec: BusyRecord;
}

const busyMap = new Map<HTMLElement, BusyRecord>();
let lastClick: { el: HTMLElement; time: number } | null = null;
let installed = false;

function isInteractive(el: Element): boolean {
  const tag = (el.tagName || "").toLowerCase();
  if (tag === "button") return true;
  if (tag === "a" && el.getAttribute("href") != null) return true;
  if (tag === "input" && (el.getAttribute("type") || "").toLowerCase() === "submit") return true;
  const role = (el.getAttribute("role") || "").toLowerCase();
  return role === "button" || role === "tab";
}

function closestInteractive(target: EventTarget | null): HTMLElement | null {
  let el = target instanceof Element ? target : null;
  while (el) {
    if (isInteractive(el)) return el as HTMLElement;
    el = el.parentElement;
  }
  return null;
}

function injectCss(): void {
  if (!document.head || document.head.querySelector("style#mod-busy-css")) return;
  const s = document.createElement("style");
  s.id = "mod-busy-css";
  s.textContent =
    ".mod-busy{position:relative;opacity:.55;box-shadow:inset 0 3px 6px rgba(0,0,0,.35);transform:translateY(1px);}" +
    ".mod-busy > .mod-busy-spinner{position:absolute;right:4px;top:50%;width:12px;height:12px;margin-top:-6px;border-radius:9999px;border:2px solid currentColor;border-top-color:transparent;animation:mod-busy-spin .8s linear infinite;}" +
    "@keyframes mod-busy-spin{to{transform:rotate(360deg);}}" +
    "@media (prefers-reduced-motion: reduce){.mod-busy > .mod-busy-spinner{animation:none;}}";
  document.head.appendChild(s);
}

function clearBusy(el: HTMLElement): void {
  const rec = busyMap.get(el);
  if (rec?.timer) clearTimeout(rec.timer);
  busyMap.delete(el);
  el.classList.remove("mod-busy");
  el.removeAttribute("aria-busy");
  const sp = el.querySelector(":scope > .mod-busy-spinner");
  if (sp) el.removeChild(sp);
}

function markBusy(el: HTMLElement): BusyRecord {
  const existing = busyMap.get(el);
  if (existing) {
    existing.count++;
    return existing;
  }
  injectCss();
  el.classList.add("mod-busy");
  el.setAttribute("aria-busy", "true");
  const sp = document.createElement("span");
  sp.className = "mod-busy-spinner";
  sp.setAttribute("aria-hidden", "true");
  el.appendChild(sp);
  const rec: BusyRecord = { count: 1, startedAt: Date.now(), spinner: sp, timer: null };
  rec.timer = setTimeout(() => {
    if (busyMap.get(el) === rec) clearBusy(el); // 60 s safety release
  }, BUSY_SAFETY_MS);
  busyMap.set(el, rec);
  return rec;
}

function onClick(e: Event): void {
  const el = closestInteractive(e.target);
  if (!el) return;
  if (busyMap.has(el)) {
    // further clicks on a busy control are swallowed
    e.preventDefault();
    e.stopPropagation();
    return;
  }
  lastClick = { el, time: Date.now() };
}

function onKey(e: KeyboardEvent): void {
  if (e.key !== "Enter" && e.key !== " ") return;
  const el = closestInteractive(e.target);
  if (!el) return;
  if (busyMap.has(el)) {
    e.preventDefault();
    return;
  }
  lastClick = { el, time: Date.now() };
}

export function installBusyIndicator(): void {
  if (installed || typeof document === "undefined") return;
  installed = true;
  document.addEventListener("click", onClick, true);
  document.addEventListener("keydown", onKey, true);
}

/** Test/teardown helper: remove listeners and drop all state. */
export function uninstallBusyIndicator(): void {
  if (typeof document !== "undefined") {
    document.removeEventListener("click", onClick, true);
    document.removeEventListener("keydown", onKey, true);
  }
  for (const el of Array.from(busyMap.keys())) clearBusy(el);
  lastClick = null;
  installed = false;
}

/** Attach a starting request to the control clicked a moment ago, or null. */
export function attachBusyRequest(url?: string): BusyToken | null {
  if (typeof document === "undefined" || !lastClick) return null;
  if (url && EXCLUDED_URL.test(url)) return null;
  if (Date.now() - lastClick.time > CLICK_ATTACH_WINDOW_MS) return null;
  const el = lastClick.el;
  if (!el.isConnected) return null;
  return { el, rec: markBusy(el) };
}

export function releaseBusyRequest(token: BusyToken | null): void {
  if (!token) return;
  const rec = busyMap.get(token.el);
  if (rec !== token.rec) return; // already released (safety) or replaced
  rec.count--;
  if (rec.count <= 0) clearBusy(token.el);
}

export function isControlBusy(el: HTMLElement): boolean {
  return busyMap.has(el);
}

installBusyIndicator();
