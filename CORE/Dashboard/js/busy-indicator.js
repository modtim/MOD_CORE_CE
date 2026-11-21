// FIX-DASHBOARD-CLICKED-CONTROL-BUSY-STATE (11-17)
// Owner Tim, verbatim: "the button should go triggered until the thing is
// done loading. That way it looks clicked" - clicks wait seconds with no
// feedback; a thin top loading bar was rejected. Design: the control the
// user just CLICKED shows a pressed/busy state until the work it started
// is done. Generic mechanism, no per-button edits:
//   1. a capture-phase click listener remembers the last clicked
//      interactive element (closest of button, a[href], [role=button],
//      [role=tab], .tab, input[type=submit]) and the time;
//   2. when fetchApi (js/api.js) starts a request within ~150 ms after
//      that click, the request attaches to that element: while at least
//      one attached request is in flight the element gets class
//      `mod-busy`, aria-busy="true", a CSS-only spinner and a visibly
//      pressed look; further clicks on it are swallowed until the count
//      returns to 0 (the 401-refresh-retry path in api.js counts as ONE
//      request - the hook only attaches on the first attempt);
//   3. no request within 150 ms: nothing is shown;
//   4. streams (EventSource / notifications) and periodic polls never
//      route through fetchApi right after a click, so they never mark;
//   5. hard 60 s safety release (setTimeout + attach-time check);
//   6. prefers-reduced-motion: no spinner animation, pressed look kept.
// Keyboard Enter/Space on a focused control record the same way.
// Self-contained classic script (api.js is parsed as a classic script in
// vm tests, so it cannot `import` this module - it reads
// window.ModBusyIndicator instead). Injects its own tiny CSS, no theme
// dependency. Shared-code UX addition; ships to paid as well.
// FIX-DASHBOARD-CLICKED-CONTROL-BUSY-STATE BEGIN
(function () {
    const CLICK_ATTACH_WINDOW_MS = 150;
    const BUSY_SAFETY_MS = 60000;

    function isInteractive(el) {
        if (!el) return false;
        const tag = (el.tagName || '').toLowerCase();
        if (tag === 'button') return true;
        if (tag === 'a' && el.getAttribute('href') != null) return true;
        if (tag === 'input' && (el.getAttribute('type') || '').toLowerCase() === 'submit') return true;
        const role = (el.getAttribute('role') || '').toLowerCase();
        if (role === 'button' || role === 'tab') return true;
        const cls = el.getAttribute('class') || '';
        return /(^|\s)tab(\s|$)/.test(cls);
    }

    function closestInteractive(target) {
        let el = target;
        while (el) {
            if (isInteractive(el)) return el;
            el = el.parentElement;
        }
        return null;
    }

    const busyMap = new Map(); // element -> {count, startedAt, spinner}

    function injectCss() {
        if (!document.head) return;
        if (document.head.querySelector('style#mod-busy-css')) return;
        const s = document.createElement('style');
        s.id = 'mod-busy-css';
        s.textContent =
            '.mod-busy{position:relative;opacity:.55;box-shadow:inset 0 3px 6px rgba(0,0,0,.35);transform:translateY(1px);}' +
            '.mod-busy > .mod-busy-spinner{position:absolute;right:4px;top:50%;width:12px;height:12px;margin-top:-6px;border-radius:9999px;border:2px solid currentColor;border-top-color:transparent;animation:mod-busy-spin .8s linear infinite;}' +
            '@keyframes mod-busy-spin{to{transform:rotate(360deg);}}' +
            '@media (prefers-reduced-motion: reduce){.mod-busy > .mod-busy-spinner{animation:none;}}';
        document.head.appendChild(s);
    }

    function markBusy(el) {
        const rec = busyMap.get(el);
        if (rec) { rec.count++; return; }
        injectCss();
        el.classList.add('mod-busy');
        el.setAttribute('aria-busy', 'true');
        const sp = document.createElement('span');
        sp.className = 'mod-busy-spinner';
        sp.setAttribute('aria-hidden', 'true');
        el.appendChild(sp);
        busyMap.set(el, { count: 1, startedAt: Date.now(), spinner: sp });
    }

    function clearBusy(el) {
        busyMap.delete(el);
        el.classList.remove('mod-busy');
        el.removeAttribute('aria-busy');
        const sp = el.querySelector('.mod-busy-spinner');
        if (sp) el.removeChild(sp);
    }

    function releaseBusy(el) {
        const rec = busyMap.get(el);
        if (!rec) return;
        rec.count--;
        if (rec.count > 0) return;
        clearBusy(el);
    }

    let lastClick = null; // {el, time}

    function swallow(el) {
        busyMap.delete(el); // safety: a stuck busy never swallows forever
        clearBusy(el);
    }

    function onClick(e) {
        const el = closestInteractive(e.target);
        if (!el) return;
        const rec = busyMap.get(el);
        if (rec) {
            // further clicks on a busy control are swallowed
            e.preventDefault();
            e.stopPropagation();
            if (Date.now() - rec.startedAt >= BUSY_SAFETY_MS) swallow(el);
            return;
        }
        lastClick = { el: el, time: Date.now() };
    }

    function onKey(e) {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        const el = closestInteractive(e.target);
        if (!el) return;
        const rec = busyMap.get(el);
        if (rec) {
            e.preventDefault();
            if (Date.now() - rec.startedAt >= BUSY_SAFETY_MS) swallow(el);
            return;
        }
        lastClick = { el: el, time: Date.now() };
    }

    document.addEventListener('click', onClick, true);
    document.addEventListener('keydown', onKey, true);

    function attachRequest() {
        if (!lastClick) return null;
        const now = Date.now();
        if (now - lastClick.time > CLICK_ATTACH_WINDOW_MS) return null;
        const el = lastClick.el;
        const rec = busyMap.get(el);
        if (rec && now - rec.startedAt >= BUSY_SAFETY_MS) {
            swallow(el); // hard 60 s safety release on attach
        }
        markBusy(el);
        setTimeout(function () {
            const r = busyMap.get(el);
            if (r && Date.now() - r.startedAt >= BUSY_SAFETY_MS) swallow(el);
        }, BUSY_SAFETY_MS);
        return el;
    }

    function releaseRequest(el) {
        if (el) releaseBusy(el);
    }

    window.ModBusyIndicator = {
        attachRequest: attachRequest,
        releaseRequest: releaseRequest,
        isBusy: function (el) { return busyMap.has(el); },
    };
})();
// FIX-DASHBOARD-CLICKED-CONTROL-BUSY-STATE END
