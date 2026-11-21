// MOD Assistant — Dashboard chat panel (mod-assistant-agent §8).
//
// The companion to assistant-launcher.js. The launcher toggles
// `<body class="assistant-panel-open">`; this script renders the
// actual panel when that class is set. Lifecycle is symmetric to
// the launcher — gated on /v1/assistant/config.enabled, injects
// once, listens for class flips.
//
// Talks to POST /v1/assistant/chat via Server-Sent Events. The
// backend is a stub at the moment (§5 replaces it with real LLM
// orchestration); event shape (text-token / action-emit / citation
// / done / error) is the v1 contract and won't change.

(function () {
    "use strict";

    // Canonical constants + nav-action runtime (mod-assistant-agent §6.4)
    // live in window.ModAssistantNav, compiled from
    // CORE/WorkflowBuilder/lib/assistant-nav/navActions.ts by
    // CORE/WorkflowBuilder/lib/assistant-nav/build-dashboard.js. Loaded by index.html
    // BEFORE this script (js/vendor/assistant-nav.js).
    const _Nav = window.ModAssistantNav;

    // Soft-fail: if the nav script 404'd (e.g. a pre-§6.4 image), window.
    // ModAssistantNav is undefined — log once and bail instead of throwing
    // on the BODY_CLASS read below.
    if (!_Nav) {
        console.warn("[mod-assistant] ModAssistantNav unavailable; panel disabled");
        return;
    }

    const BODY_CLASS = _Nav.BODY_CLASS;
    const STORAGE_WIDTH_KEY = "mod.assistantPanelWidth";
    const SESSION_ID_KEY = "mod.assistantSessionId";
    const PANEL_ID = "mod-assistant-panel";

    // Persist the user's open/closed choice to the SHARED flag (localStorage +
    // parent-domain cookie) and broadcast so a live launcher (this app or the
    // sibling app in another tab) updates immediately. Counterpart to the
    // launcher's safeWriteOpenFlag — the panel's own close button has to write
    // the SAME winning cookie or the choice won't stick across navigation.
    function _persistOpenFlag(open) {
        _Nav.safeWriteOpenFlag(open);
        var relay = _Nav.createBroadcastRelay();
        relay.postOpenState(open);
        relay.close();
    }

    const DEFAULT_WIDTH = 400;
    const MIN_WIDTH = 320;
    const MAX_WIDTH = 720;

    const SUGGESTED_PROMPTS = [
        "Create a new node",
        "Show me my usage",
        "Show me my workers",
    ];

    let messagesEl = null;
    let inputEl = null;
    let sendBtn = null;
    let panelEl = null;
    let scrollGuard = { atBottom: true };
    let jumpPillEl = null;

    // ---------- session id (per-tab) ---------------------------------------

    function _sessionId() {
        try {
            let sid = sessionStorage.getItem(SESSION_ID_KEY);
            if (!sid) {
                sid = (crypto && crypto.randomUUID)
                    ? crypto.randomUUID()
                    : "s-" + Date.now() + "-" + Math.random().toString(36).slice(2);
                sessionStorage.setItem(SESSION_ID_KEY, sid);
            }
            return sid;
        } catch (_) {
            return "s-" + Date.now();
        }
    }

    // ---------- width persistence ------------------------------------------

    function _readWidth() {
        try {
            const raw = parseInt(localStorage.getItem(STORAGE_WIDTH_KEY) || "", 10);
            if (!isNaN(raw) && raw >= MIN_WIDTH && raw <= MAX_WIDTH) return raw;
        } catch (_) { /* noop */ }
        return DEFAULT_WIDTH;
    }

    function _writeWidth(w) {
        try { localStorage.setItem(STORAGE_WIDTH_KEY, String(w)); } catch (_) {}
    }

    // ---------- DOM helpers -----------------------------------------------

    function _el(tag, className, text) {
        const el = document.createElement(tag);
        if (className) el.className = className;
        if (text != null) el.textContent = text;
        return el;
    }

    function _isAtBottom() {
        if (!messagesEl) return true;
        const buffer = 24;
        return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < buffer;
    }

    function _scrollToBottom() {
        if (!messagesEl) return;
        messagesEl.scrollTop = messagesEl.scrollHeight;
        scrollGuard.atBottom = true;
        if (jumpPillEl) jumpPillEl.classList.add("hidden");
    }

    // ---------- message rendering -----------------------------------------

    function _renderUserMessage(text) {
        const wrap = _el("div", "mod-asst-msg mod-asst-msg-user");
        const bubble = _el("div", "mod-asst-bubble", text);
        wrap.appendChild(bubble);
        messagesEl.appendChild(wrap);
        if (scrollGuard.atBottom) _scrollToBottom();
        else if (jumpPillEl) jumpPillEl.classList.remove("hidden");
        return wrap;
    }

    function _renderAssistantMessage() {
        const wrap = _el("div", "mod-asst-msg mod-asst-msg-assistant");
        const bubble = _el("div", "mod-asst-bubble");
        const thinking = _el("span", "mod-asst-thinking", "thinking…");
        bubble.appendChild(thinking);
        const actionsEl = _el("div", "mod-asst-actions");
        const citationsEl = _el("div", "mod-asst-citations");
        wrap.appendChild(bubble);
        wrap.appendChild(actionsEl);
        wrap.appendChild(citationsEl);
        messagesEl.appendChild(wrap);
        if (scrollGuard.atBottom) _scrollToBottom();
        return {
            wrap,
            bubble,
            thinking,
            actionsEl,
            citationsEl,
            textBuf: "",
            textNode: null,
        };
    }

    function _appendToken(handle, token) {
        if (handle.thinking) {
            handle.thinking.remove();
            handle.thinking = null;
            handle.textNode = document.createTextNode("");
            handle.bubble.appendChild(handle.textNode);
        }
        handle.textBuf += token;
        handle.textNode.nodeValue = handle.textBuf;
        if (scrollGuard.atBottom) _scrollToBottom();
        else if (jumpPillEl) jumpPillEl.classList.remove("hidden");
    }

    // The API's SSE stream emits `progress` events (assistant_orchestrator.py
    // `_sse("progress", {"message": ...})`) while it wakes a sleeping AI
    // stack, so the user sees something during the 30-90s wake. The pending
    // bubble's muted "thinking…" slot doubles as the status line: a second
    // progress event replaces the text instead of appending, and the first
    // text token (or an error) removes it. textContent only — server text
    // is never parsed as markup.
    function _renderProgress(handle, parsed) {
        const msg = String(parsed.message || "");
        if (!msg) return;
        if (handle.thinking) {
            handle.thinking.textContent = msg;
            return;
        }
        if (handle.textNode) return; // the answer already started — no status line
        const line = _el("span", "mod-asst-thinking", msg);
        handle.bubble.appendChild(line);
        handle.thinking = line;
    }

    function _openEscalateDialog() {
        if (document.querySelector(".mod-asst-escalate-form")) return;
        const wrap = _el("div", "mod-asst-msg mod-asst-msg-system");
        const form = _el("form", "mod-asst-escalate-form");
        const title = _el(
            "div", "mod-asst-escalate-title", "Contact human support",
        );
        const subjInput = _el("input", "mod-asst-escalate-input");
        subjInput.type = "text";
        subjInput.placeholder = "Short subject (e.g. 'Workers stuck')";
        subjInput.required = true;
        subjInput.maxLength = 200;
        const msgInput = _el("textarea", "mod-asst-escalate-textarea");
        msgInput.placeholder = "What's happening? Your chat transcript is attached automatically.";
        msgInput.required = true;
        msgInput.rows = 3;
        msgInput.maxLength = 2000;
        const sevLabel = _el("label", "mod-asst-escalate-sev-label", "Severity:");
        const sevSelect = _el("select", "mod-asst-escalate-select");
        const optNormal = _el("option", "", "Normal");
        optNormal.value = "normal";
        const optEmergency = _el(
            "option", "", "Emergency — service down / data loss",
        );
        optEmergency.value = "emergency";
        sevSelect.appendChild(optNormal);
        sevSelect.appendChild(optEmergency);
        const row = _el("div", "mod-asst-escalate-row");
        row.appendChild(sevLabel);
        row.appendChild(sevSelect);
        const submit = _el("button", "mod-asst-action-btn", "Send to support");
        submit.type = "submit";
        const cancel = _el("button", "mod-asst-foot-link", "Cancel");
        cancel.type = "button";
        cancel.addEventListener("click", function () { wrap.remove(); });
        const btnRow = _el("div", "mod-asst-escalate-row");
        btnRow.appendChild(submit);
        btnRow.appendChild(cancel);

        form.appendChild(title);
        form.appendChild(subjInput);
        form.appendChild(msgInput);
        form.appendChild(row);
        form.appendChild(btnRow);

        form.addEventListener("submit", async function (e) {
            e.preventDefault();
            submit.disabled = true;
            submit.textContent = "Sending…";
            try {
                const token = (function () {
                    try { return sessionStorage.getItem("accessToken"); }
                    catch (_) { return null; }
                })();
                const apiBase = (window.APP_CONFIG && window.APP_CONFIG.API_URL) || "";
                const headers = { "Content-Type": "application/json" };
                if (token) headers["Authorization"] = `Bearer ${token}`;
                const resp = await fetch(`${apiBase}/assistant/escalate`, {
                    method: "POST", headers,
                    body: JSON.stringify({
                        session_id: _sessionId(),
                        subject: subjInput.value,
                        message: msgInput.value,
                        severity: sevSelect.value,
                    }),
                });
                if (!resp.ok) {
                    const text = await resp.text();
                    throw new Error("HTTP " + resp.status + ": " + text.slice(0, 200));
                }
                const data = await resp.json();
                wrap.remove();
                _renderEscalateSuccess({
                    recipient: data.recipient,
                    severity: data.severity,
                    tenant_admin_count: data.tenant_admin_count,
                    subject: subjInput.value,
                    ok: data.ok,
                    message: data.message,
                });
            } catch (err) {
                submit.disabled = false;
                submit.textContent = "Send to support";
                _renderErrorRow(
                    "Could not send: " + (err && err.message ? err.message : "unknown error"),
                );
            }
        });

        wrap.appendChild(form);
        messagesEl.appendChild(wrap);
        if (scrollGuard.atBottom) _scrollToBottom();
        subjInput.focus();
    }

    function _renderEscalateSuccess(data) {
        const wrap = _el("div", "mod-asst-msg mod-asst-msg-system");
        const actionsEl = _el("div", "mod-asst-actions");
        const handle = { actionsEl: actionsEl };
        _renderSupportContactedRow(handle, {
            action: "support-contacted",
            recipient: data.recipient,
            severity: data.severity,
            tenant_admin_count: data.tenant_admin_count,
            subject: data.subject,
        });
        wrap.appendChild(actionsEl);
        messagesEl.appendChild(wrap);
        if (scrollGuard.atBottom) _scrollToBottom();
    }

    function _renderSupportContactedRow(handle, action) {
        // Receipt row — NOT a button. Confirms to the user the
        // assistant successfully shipped a support escalation email
        // (from the contact_support tool OR the footer-link
        // /assistant/escalate endpoint). Distinct visual treatment so
        // it doesn't look like a clickable action.
        const row = _el("div", "mod-asst-support-receipt");
        const icon = _el("span", "mod-asst-support-receipt-icon", "✓");
        const body = _el("div", "mod-asst-support-receipt-body");
        const headline = _el(
            "div", "mod-asst-support-receipt-headline",
            action.severity === "emergency"
                ? "Support contacted — flagged emergency"
                : "Support contacted",
        );
        const detail = _el(
            "div", "mod-asst-support-receipt-detail",
            "Transcript emailed to " + (action.recipient || "your address") +
            (action.tenant_admin_count
                ? " (cc'd " + action.tenant_admin_count +
                  " tenant admin" + (action.tenant_admin_count === 1 ? "" : "s") + ")"
                : ""),
        );
        body.appendChild(headline);
        body.appendChild(detail);
        if (action.subject) {
            const subj = _el(
                "div", "mod-asst-support-receipt-subject",
                "Subject: " + action.subject,
            );
            body.appendChild(subj);
        }
        row.appendChild(icon);
        row.appendChild(body);
        handle.actionsEl.appendChild(row);
        if (scrollGuard.atBottom) _scrollToBottom();
    }

    function _renderActionEmit(handle, action) {
        // support-contacted is a status receipt, not a clickable
        // button — render it separately and bail.
        if (action.action === "support-contacted") {
            _renderSupportContactedRow(handle, action);
            return;
        }
        // Action button is intentionally visually distinct from the
        // surrounding text so the user knows they are *choosing* to
        // execute, not reading prose. Click runs the local
        // navigation runtime (see _executeAction below).
        const btn = _el("button", "mod-asst-action-btn");
        btn.type = "button";
        btn.textContent = _Nav.actionLabel(action);
        btn.addEventListener("click", function () {
            _executeAction(action);
        });
        handle.actionsEl.appendChild(btn);
        if (scrollGuard.atBottom) _scrollToBottom();
    }

    function _renderCitation(handle, citation) {
        const link = _el("a", "mod-asst-citation");
        link.href = citation.url || "#";
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = "Source: " + (citation.title || "docs");
        handle.citationsEl.appendChild(link);
    }

    function _renderErrorRow(message) {
        const wrap = _el("div", "mod-asst-msg mod-asst-msg-error");
        const bubble = _el("div", "mod-asst-bubble", message || "Something went wrong.");
        wrap.appendChild(bubble);
        // An expired/invalid confirm token (services/assistant_confirm.py
        // :316/:320) gets NO Retry: retrying would re-run the turn without a token,
        // and the single-use token cannot be re-minted client-side. Offer nothing
        // automatic; the user re-asks explicitly.
        const tokenish = /confirm token/i.test(message || "");
        if (!tokenish) {
            const retry = _el("button", "mod-asst-action-btn", "Retry");
            retry.type = "button";
            retry.addEventListener("click", function () {
                const last = _lastUserMessage();
                if (last) _sendMessage(last);
            });
            wrap.appendChild(retry);
        }
        messagesEl.appendChild(wrap);
        if (scrollGuard.atBottom) _scrollToBottom();
    }

    // ---------- Confirmation dialog ----------------------------
    //
    // The orchestrator refuses a risky tool call and answers with the gate
    // envelope from services/assistant_confirm.py:check_tool_call
    //   {allowed:false, needs_confirmation:true, confirm_token, summary,
    //    risk_category, forced_by?}
    // and the request field to re-send it under is `confirm_tokens`
    // (services/assistant_orchestrator.py:1858 — {tool_name: token}).
    // Tokens are single-use: they live in this closure map and are cleared the
    // moment Confirm fires the re-send. Nothing is ever cached or replayed.
    var pendingConfirms = [];
    var confirmUntrustedNote = false;
    var confirmTokens = {};
    var confirmDialogEl = null;
    var confirmKeyHandler = null;

    function _confirmSummary(tool, args, gateSummary) {
        const named = (gateSummary || "").trim();
        if (named) return named;
        const pairs = [];
        if (args && typeof args === "object") {
            const keys = Object.keys(args);
            for (const k of keys) {
                const v = String(args[k] == null ? "" : args[k]);
                pairs.push(k + "=" + (v.length > 60 ? v.slice(0, 60) + "…" : v));
            }
        }
        return "assistant wants to run " + tool + ": "
            + (pairs.length ? pairs.join(", ") : "(no args)");
    }

    function _openConfirmDialog() {
        if (confirmDialogEl && document.querySelector(
            ".mod-asst-confirm-dialog")) return;
        const overlay = _el("div", "mod-asst-confirm-dialog");
        overlay.setAttribute("role", "dialog");
        overlay.setAttribute("aria-modal", "true");
        overlay.setAttribute("tabindex", "-1");
        const title = _el("div", "mod-asst-confirm-title",
            "Confirm before the assistant acts");
        title.id = "mod-asst-confirm-title";
        overlay.appendChild(title);
        // aria-labelledby points at the title so the dialog is announced.
        overlay.setAttribute("aria-labelledby", title.id);

        if (confirmUntrustedNote) {
            const note = _el("p", "mod-asst-confirm-note",
                "This turn included content from outside MOD (logs, docs, or your "
                + "own uploads), which the assistant must treat as data, not "
                + "instructions. Anything that writes or sends now needs your "
                + "confirmation.");
            overlay.appendChild(note);
        }

        for (const c of pendingConfirms) {
            const row = _el("div", "mod-asst-confirm-row");
            // Every value is textContent — model-authored tool args can never
            // become markup.
            const nameEl = _el("div", "mod-asst-confirm-tool", c.tool);
            const sumEl = _el("div", "mod-asst-confirm-summary", c.summary);
            row.appendChild(nameEl);
            row.appendChild(sumEl);
            if (c.forcedBy === "untrusted_content") {
                row.appendChild(_el("div", "mod-asst-confirm-forced",
                    "Forced by untrusted content in this turn."));
            }
            overlay.appendChild(row);
        }

        const confirmBtn = _el("button", "mod-asst-confirm-btn", "Confirm");
        confirmBtn.type = "button";
        confirmBtn.addEventListener("click", function () { _confirmAndResend(); });
        const cancelBtn = _el("button", "mod-asst-foot-link", "Cancel");
        cancelBtn.type = "button";
        cancelBtn.addEventListener("click", function () { _cancelConfirm(); });
        const btnRow = _el("div", "mod-asst-confirm-row");
        btnRow.appendChild(confirmBtn);
        btnRow.appendChild(cancelBtn);
        overlay.appendChild(btnRow);

        document.body.appendChild(overlay);
        confirmDialogEl = overlay;

        // Focus trap (Tab stays inside the dialog) comes from the shared
        // Dashboard a11y helper; Escape is ours so it can render the
        // "Action cancelled" row instead of just hiding the overlay.
        if (window.modA11y) {
            window.modA11y.attach(overlay, { role: "dialog", dismissible: false });
        }
        if (!confirmKeyHandler) {
            confirmKeyHandler = function (e) {
                if (!confirmDialogEl || !confirmDialogEl.isConnected) return;
                if (e.key === "Escape") {
                    e.stopPropagation();
                    _cancelConfirm();
                }
            };
            document.addEventListener("keydown", confirmKeyHandler, true);
        }
    }

    function _closeConfirmDialog() {
        if (confirmKeyHandler) {
            document.removeEventListener("keydown", confirmKeyHandler, true);
            confirmKeyHandler = null;
        }
        if (confirmDialogEl) {
            if (window.modA11y) window.modA11y.detach(confirmDialogEl);
            confirmDialogEl.remove();
            confirmDialogEl = null;
        }
    }

    function _cancelConfirm() {
        // Cancel sends NOTHING: the tokens are dropped here so they can never
        // be replayed, and no request is issued.
        confirmTokens = {};
        pendingConfirms = [];
        confirmUntrustedNote = false;
        _closeConfirmDialog();
        const wrap = _el("div", "mod-asst-msg mod-asst-msg-assistant");
        wrap.appendChild(_el("div", "mod-asst-bubble", "Action cancelled"));
        messagesEl.appendChild(wrap);
        if (scrollGuard.atBottom) _scrollToBottom();
    }

    async function _confirmAndResend() {
        // Nothing pending (dialog already closed by a first Confirm, or a
        // stale listener firing late): send nothing. A Confirm with no
        // pending gate must never re-run the turn unconfirmed.
        if (!pendingConfirms.length) return;
        // Take the tokens exactly once, then clear: single-use.
        const tokens = confirmTokens;
        confirmTokens = {};
        pendingConfirms = [];
        confirmUntrustedNote = false;
        _closeConfirmDialog();
        const last = _lastUserMessage();
        if (last) _sendMessage(last, tokens);
    }

    function _lastUserMessage() {
        const rows = messagesEl.querySelectorAll(".mod-asst-msg-user .mod-asst-bubble");
        return rows.length ? rows[rows.length - 1].textContent : null;
    }

    function _renderEmptyState() {
        if (messagesEl.querySelector(".mod-asst-empty")) return;
        const wrap = _el("div", "mod-asst-empty");
        const headline = _el("p", "mod-asst-empty-headline",
            "Hi! I'm MOD Assistant. Ask me where to go, how to do something, or why a run failed.");
        wrap.appendChild(headline);
        SUGGESTED_PROMPTS.forEach(function (prompt) {
            const chip = _el("button", "mod-asst-suggested");
            chip.type = "button";
            chip.textContent = prompt;
            chip.addEventListener("click", function () {
                _sendMessage(prompt);
            });
            wrap.appendChild(chip);
        });
        messagesEl.appendChild(wrap);
    }

    function _clearEmptyState() {
        const empty = messagesEl.querySelector(".mod-asst-empty");
        if (empty) empty.remove();
    }

    function _startNewConversation() {
        // Drop the per-tab session id so the backend allocates a
        // fresh AssistantSession on the next turn. Clear the panel
        // and render the empty state.
        try { sessionStorage.removeItem(SESSION_ID_KEY); } catch (_) {}
        if (messagesEl) {
            while (messagesEl.firstChild) messagesEl.removeChild(messagesEl.firstChild);
        }
        scrollGuard.atBottom = true;
        if (jumpPillEl) jumpPillEl.classList.add("hidden");
        _renderEmptyState();
        if (inputEl) {
            inputEl.value = "";
            inputEl.focus();
        }
    }

    // Resume the user's most-recent server session so the conversation
    // carries across page navigation AND across the Dashboard/Builder
    // subdomain boundary (the session is server-owned per user; sessionStorage
    // can't cross origins). Adopt the session id + repaint the transcript.
    // Best-effort + only when the panel is still showing the empty state, so a
    // transient failure or an in-progress chat is never disturbed.
    async function _resumeHistory() {
        var token;
        try { token = sessionStorage.getItem("accessToken"); }
        catch (_) { token = null; }
        if (!token) return;
        try {
            var apiBase = (window.APP_CONFIG && window.APP_CONFIG.API_URL) || "";
            var resp = await fetch(apiBase + "/assistant/history", {
                headers: { Authorization: "Bearer " + token },
            });
            if (!resp || !resp.ok) return;
            var data = await resp.json();
            if (!data || !data.session_id ||
                !Array.isArray(data.messages) || data.messages.length === 0) return;
            // Only repaint a still-empty panel (don't clobber a live chat).
            if (!messagesEl || !messagesEl.querySelector(".mod-asst-empty")) return;
            try { sessionStorage.setItem(SESSION_ID_KEY, data.session_id); } catch (_) {}
            _clearEmptyState();
            data.messages.forEach(function (m) {
                if (m && m.role === "assistant") {
                    _appendToken(_renderAssistantMessage(), (m.content || ""));
                } else if (m) {
                    _renderUserMessage(m.content || "");
                }
            });
            _scrollToBottom();
        } catch (_) { /* transient — leave the empty state */ }
    }

    // ---------- client-side action runtime --------------------------------
    //
    // §6's full vocabulary lands later — this is a minimum runtime
    // for navigate / open-new-tab / open-dialog / chain so the §8
    // panel's action buttons actually do something useful right
    // now. `open-dialog` is dispatched as a custom window event so
    // page-level code can decide how to surface it.

    // Cross-subdomain handoff for chain actions that span apps.
    // Delegates to the shared runtime (§6.4) so the cookie name/shape
    // can't drift from the WorkflowBuilder panel's copy.
    function _writePendingActions(actions) {
        _Nav.writePendingActions(actions);
    }

    function _readAndClearPendingActions() {
        return _Nav.readAndClearPendingActions();
    }

    // Resolve a navigate URL to its target subdomain when the action
    // declares a different app than this one.
    var _THIS_APP = "dashboard";

    function _appendAuthTokensForWb(targetUrl) {
        // WB and Dashboard live on different subdomains; sessionStorage
        // doesn't share across origins. Dashboard's existing
        // "Open Workflow Builder" flow (js/ui.js) hand-offs by
        // appending tokens. GSEC R-074: tokens ride in the URL
        // FRAGMENT (#token=<access>&refresh_token=<refresh>) — never sent
        // to the server, so never in access logs; query params stay in the
        // query string. Without these, WB's useWorkflowApi sees no token
        // and auto-redirects back to Dashboard — the "bounce" Tim hit.
        try {
            var access = sessionStorage.getItem("accessToken");
            var refresh = sessionStorage.getItem("refreshToken");
            if (!access) return targetUrl;
            var sep = targetUrl.indexOf("#") >= 0 ? "&" : "#";
            var withToken = targetUrl + sep + "token=" + encodeURIComponent(access);
            if (refresh) {
                withToken += "&refresh_token=" + encodeURIComponent(refresh);
            }
            return withToken;
        } catch (_) {
            return targetUrl;
        }
    }

    function _resolveNavigateUrl(action) {
        // Cross-app URLs come from runtime config (APP_CONFIG.* — set
        // by Dashboard/inject-config.sh at container start from
        // BASE_DOMAIN + WORKFLOWS_SUBDOMAIN env). Never hard-coded.
        var url = action.url || "";
        var app = action.app || "";
        if (!app || app === _THIS_APP) return url;
        var cfg = window.APP_CONFIG || {};
        if (app === "workflow_builder") {
            if (!cfg.WORKFLOWS_URL) {
                console.error(
                    "[assistant] navigate to workflow_builder requested " +
                    "but window.APP_CONFIG.WORKFLOWS_URL is empty. " +
                    "Check Dashboard/inject-config.sh + WORKFLOWS_SUBDOMAIN env.",
                );
                return null;
            }
            return _appendAuthTokensForWb(cfg.WORKFLOWS_URL + url);
        }
        if (app === "dashboard") {
            if (!cfg.DASHBOARD_URL) {
                console.error(
                    "[assistant] navigate to dashboard requested but " +
                    "window.APP_CONFIG.DASHBOARD_URL is empty.",
                );
                return null;
            }
            return cfg.DASHBOARD_URL + url;
        }
        return url;
    }

    // Thin wrapper over the canonical shared implementation (§6.4,
    // WorkflowBuilder/lib/assistant-nav/navActions.ts). Cross-app URL resolution
    // stays app-specific (runtime window.APP_CONFIG + WB auth-token
    // handoff) and is passed in as a callback.
    function _executeAction(action) {
        _Nav.executeAction(action, { resolveNavigateUrl: _resolveNavigateUrl });
    }

    // ---------- SSE client -------------------------------------------------

    async function _sendMessage(text, confirmTokensArg) {
        const trimmed = (text || "").trim();
        if (!trimmed) return;
        // The one-time tokens the user just agreed to. Consumed
        // here; the caller clears its own reference. Never stored on the panel.
        const tokens = (confirmTokensArg && Object.keys(confirmTokensArg).length)
            ? confirmTokensArg : null;
        // What this turn needs confirmed, collected from the stream. Local so
        // a second send can never inherit a stale token. `sink` is threaded
        // through _consumeStream/_dispatchSseBlock because the SSE parser is
        // shared and must stay stateless across concurrent sends.
        const sink = { pending: [], untrusted: false, tokens: {} };
        _clearEmptyState();
        _renderUserMessage(trimmed);
        inputEl.value = "";
        sendBtn.disabled = true;
        const handle = _renderAssistantMessage();

        try {
            const body = JSON.stringify({
                message: trimmed,
                session_id: _sessionId(),
                // Dashboard is hash-routed (location.pathname is always
                // "/"); pathname + hash gives the actual user-facing
                // URL so the orchestrator can ground "current page"
                // correctly against the site map.
                current_page: location.pathname + (location.hash || ""),
                // Disambiguates URLs that exist in both apps ("/" lives
                // in both Dashboard and WorkflowBuilder site-map
                // entries). Without this, page_for_url("/") returned
                // the WB canvas and the wrap thought the user was
                // already there.
                current_app: "dashboard",
                // The one-time tokens the user just agreed to.
                // Omitted when there are none so ordinary turns keep the
                // exact pre-confirm-gate request shape.
                ...(tokens ? { confirm_tokens: tokens } : {}),
            });
            // Dashboard's auth lives in sessionStorage.accessToken — the
            // same pattern settings.js / billing.js / etc. use. Build
            // a Bearer header inline.
            const token = (function () {
                try { return sessionStorage.getItem("accessToken"); }
                catch (_) { return null; }
            })();
            const apiBase = (window.APP_CONFIG && window.APP_CONFIG.API_URL) || "";
            const headers = { "Content-Type": "application/json" };
            if (token) headers["Authorization"] = `Bearer ${token}`;
            const resp = await fetch(`${apiBase}/assistant/chat`, {
                method: "POST", headers, body,
            });
            if (!resp || !resp.ok || !resp.body) {
                throw new Error(resp ? `HTTP ${resp.status}` : "no response");
            }
            await _consumeStream(resp.body, handle, sink);
            // FX.72 — the API labels every assistant reply with an
            // X-MOD-AI-Generated response header (GY.V10); surface it once
            // the reply is fully rendered, inside the wrap (after the bubble)
            // so it never interleaves with streamed text.
            if (resp.headers.get("X-MOD-AI-Generated") === "true") {
                handle.wrap.appendChild(
                    _el("span", "mod-asst-ai-badge", "AI-generated"));
            }
            // The turn ended with refusals: open the dialog.
            if (sink.pending.length > 0) {
                pendingConfirms = sink.pending;
                confirmUntrustedNote = sink.untrusted;
                confirmTokens = sink.tokens;
                _openConfirmDialog();
            }
        } catch (err) {
            console.warn("[assistant-panel] chat failed:", err);
            // Replace the in-flight assistant bubble with an error row.
            handle.wrap.remove();
            _renderErrorRow("Network or server error. Click Retry.");
        } finally {
            sendBtn.disabled = false;
            inputEl.focus();
        }
    }

    async function _consumeStream(body, handle, sink) {
        const reader = body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buf = "";
        // The SSE wire format is event-blocks separated by blank
        // lines. Each block has "event: <name>\n" + "data: <json>\n".
        // We split on "\n\n" and parse each block.
        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buf.indexOf("\n\n")) >= 0) {
                const block = buf.slice(0, idx);
                buf = buf.slice(idx + 2);
                _dispatchSseBlock(block, handle, sink);
            }
        }
    }

    function _dispatchSseBlock(block, handle, sink) {
        let event = "message";
        let data = "";
        block.split("\n").forEach(function (line) {
            if (line.startsWith("event:")) event = line.slice(6).trim();
            else if (line.startsWith("data:")) data += line.slice(5).trim();
        });
        if (!data) return;
        let parsed;
        try { parsed = JSON.parse(data); } catch (_) { return; }
        if (event === "text-token") _appendToken(handle, parsed.text || "");
        else if (event === "action-emit") _renderActionEmit(handle, parsed);
        else if (event === "citation") _renderCitation(handle, parsed);
        else if (event === "session") { /* just an ack */ }
        else if (event === "progress") _renderProgress(handle, parsed);
        else if (event === "error") {
            // An expired/invalid confirm token is shown verbatim
            // and offers nothing automatic (see _renderErrorRow).
            // A wake-up progress line still in the pending bubble is dropped
            // — the answer never comes, so a stale status would lie.
            if (handle.thinking) {
                handle.thinking.remove();
                handle.thinking = null;
            }
            _renderErrorRow(parsed.message);
        } else if (event === "needs-confirmation"
            || (event === "tool-call" && parsed.needs_confirmation === true)) {
            // The gate envelope. Collected into `sink` (per-send
            // state) so concurrent sends can't cross-contaminate.
            const name = String(parsed.name || parsed.tool || "");
            if (!name) return;
            const forced = parsed.forced_by === "untrusted_content"
                || parsed.risk_category === "untrusted_context";
            if (forced) sink.untrusted = true;
            sink.pending.push({
                tool: name,
                summary: _confirmSummary(
                    name, parsed.args,
                    typeof parsed.summary === "string" ? parsed.summary : undefined,
                ),
                forcedBy: forced ? "untrusted_content" : null,
            });
            // The token is held ONLY in the per-send sink; cleared on send.
            if (typeof parsed.confirm_token === "string" && parsed.confirm_token) {
                sink.tokens[name] = parsed.confirm_token;
            }
        }
        // unknown event types silently dropped (defense per
        // mod-assistant-navigation-actions/spec.md)
    }

    // ---------- panel DOM --------------------------------------------------

    function _setPanelWidth(panel, w) {
        panel.style.width = w + "px";
        // Mirror the width to a body-level CSS variable so the
        // push-content rule on <body> stays in sync as the user
        // drags the resize handle.
        document.body.style.setProperty("--mod-asst-panel-w", w + "px");
    }

    function _buildPanel() {
        const panel = _el("aside", "mod-asst-panel");
        panel.id = PANEL_ID;
        panel.setAttribute("role", "complementary");
        panel.setAttribute("aria-label", "MOD Assistant");
        _setPanelWidth(panel, _readWidth());

        const resizeHandle = _el("div", "mod-asst-resize");
        resizeHandle.setAttribute("role", "separator");
        resizeHandle.setAttribute("aria-orientation", "vertical");
        panel.appendChild(resizeHandle);

        const header = _el("div", "mod-asst-header");
        const titleSpan = _el("span", "mod-asst-title", "MOD Assistant");
        // FIX-THEME-GLINDA (owner 11-17, D75): the panel title is the product
        // NAME - theme it via the ONE hook (data-theme-key; literal stays the
        // flag-OFF fallback byte for byte). The panel mounts after the page
        // render, so re-run the idempotent applier over the document here.
        titleSpan.setAttribute("data-theme-key", "assistant-panel");
        header.appendChild(titleSpan);
        const headerActions = _el("div", "mod-asst-header-actions");

        const newBtn = _el("button", "mod-asst-icon-btn", "New Conversation");
        newBtn.type = "button";
        newBtn.title = "Start a new conversation";
        newBtn.addEventListener("click", _startNewConversation);
        headerActions.appendChild(newBtn);

        const closeBtn = _el("button", "mod-asst-icon-btn", "×");
        closeBtn.type = "button";
        closeBtn.title = "Close (Esc)";
        closeBtn.addEventListener("click", function () {
            document.body.classList.remove(BODY_CLASS);
            _persistOpenFlag(false);
        });
        headerActions.appendChild(closeBtn);

        header.appendChild(headerActions);
        panel.appendChild(header);

        messagesEl = _el("div", "mod-asst-messages");
        messagesEl.addEventListener("scroll", function () {
            scrollGuard.atBottom = _isAtBottom();
            if (scrollGuard.atBottom && jumpPillEl) jumpPillEl.classList.add("hidden");
        });
        panel.appendChild(messagesEl);

        jumpPillEl = _el("button", "mod-asst-jump-pill hidden", "Jump to latest");
        jumpPillEl.type = "button";
        jumpPillEl.addEventListener("click", _scrollToBottom);
        panel.appendChild(jumpPillEl);

        const form = _el("form", "mod-asst-form");
        inputEl = _el("input", "mod-asst-input");
        inputEl.type = "text";
        inputEl.placeholder = "Ask me anything…";
        inputEl.autocomplete = "off";
        sendBtn = _el("button", "mod-asst-send", "Send");
        sendBtn.type = "submit";
        form.appendChild(inputEl);
        form.appendChild(sendBtn);
        form.addEventListener("submit", function (e) {
            e.preventDefault();
            _sendMessage(inputEl.value);
        });
        panel.appendChild(form);

        // Footer: direct "Contact human support" link for users who
        // don't want to chat first. POSTs the current session id +
        // a short note to /assistant/escalate, which calls the same
        // escalate_assistant_session helper the LLM tool uses. The
        // tool path and this path can't drift.
        const footer = _el("div", "mod-asst-foot");
        const escalateLink = _el(
            "button", "mod-asst-foot-link", "Contact human support",
        );
        escalateLink.type = "button";
        escalateLink.addEventListener("click", _openEscalateDialog);
        footer.appendChild(escalateLink);
        panel.appendChild(footer);

        // Resize: drag the left edge to widen / shrink. The handle
        // is a 6px wide strip; mouse events go to document so the
        // drag survives a brief cursor exit.
        let dragging = false;
        let startX = 0;
        let startW = 0;
        resizeHandle.addEventListener("mousedown", function (e) {
            dragging = true;
            startX = e.clientX;
            startW = panel.getBoundingClientRect().width;
            document.body.style.cursor = "col-resize";
            e.preventDefault();
        });
        document.addEventListener("mousemove", function (e) {
            if (!dragging) return;
            const delta = startX - e.clientX;
            let next = Math.round(startW + delta);
            next = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, next));
            _setPanelWidth(panel, next);
        });
        document.addEventListener("mouseup", function () {
            if (!dragging) return;
            dragging = false;
            document.body.style.cursor = "";
            const w = parseInt(panel.style.width, 10);
            if (!isNaN(w)) _writeWidth(w);
        });

        return panel;
    }

    // ---------- CSS (single <style>) --------------------------------------

    const CSS = `
.mod-asst-panel {
    position: fixed; right: 0; top: 10vh;
    height: 80vh;
    width: var(--mod-asst-panel-w, 400px);
    background: hsl(var(--card, 0 0% 100%));
    color: hsl(var(--card-foreground, 0 0% 12%));
    border: 1px solid hsl(var(--border, 220 13% 91%));
    border-right: none;
    border-top-left-radius: 12px;
    border-bottom-left-radius: 12px;
    display: none;
    flex-direction: column;
    z-index: 60;
    box-shadow: -8px 4px 24px -8px rgba(0,0,0,0.15);
    font-family: inherit;
}
body.assistant-panel-open .mod-asst-panel { display: flex; }
/* Push body content so the panel doesn't overlap the page UI. */
body.assistant-panel-open { padding-right: var(--mod-asst-panel-w, 400px); }
.mod-asst-resize {
    position: absolute; left: -3px; top: 0; bottom: 0;
    width: 6px; cursor: col-resize;
}
.mod-asst-resize:hover { background: hsl(var(--accent, 232 96% 42%) / 0.2); }
.mod-asst-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 16px;
    border-bottom: 1px solid hsl(var(--border, 220 13% 91%));
    flex-shrink: 0;
}
.mod-asst-title { font-weight: 600; font-size: 14px; }
.mod-asst-header-actions { display: flex; align-items: center; gap: 4px; }
.mod-asst-icon-btn {
    background: transparent; border: 0; line-height: 1;
    color: inherit; cursor: pointer; padding: 4px 8px;
    border-radius: 6px; font-size: 13px;
}
.mod-asst-icon-btn:hover { background: hsl(var(--muted, 220 13% 96%)); }
.mod-asst-messages {
    flex: 1; overflow-y: auto; padding: 16px;
    display: flex; flex-direction: column; gap: 12px;
}
.mod-asst-msg { display: flex; flex-direction: column; max-width: 90%; }
.mod-asst-msg-user { align-self: flex-end; align-items: flex-end; }
.mod-asst-msg-assistant { align-self: flex-start; align-items: flex-start; }
.mod-asst-msg-error { align-self: stretch; align-items: stretch; max-width: 100%; }
.mod-asst-bubble {
    padding: 8px 12px; border-radius: 12px; font-size: 13px;
    line-height: 1.5; white-space: pre-wrap; word-wrap: break-word;
}
.mod-asst-msg-user .mod-asst-bubble {
    background: hsl(var(--primary, 232 96% 42%));
    color: hsl(var(--primary-foreground, 0 0% 100%));
}
.mod-asst-msg-assistant .mod-asst-bubble {
    background: hsl(var(--muted, 220 13% 96%));
    color: hsl(var(--foreground, 0 0% 12%));
}
.mod-asst-msg-error .mod-asst-bubble {
    background: hsl(var(--destructive, 0 84% 60%) / 0.15);
    color: hsl(var(--foreground, 0 0% 12%));
    border: 1px solid hsl(var(--destructive, 0 84% 60%) / 0.4);
}
.mod-asst-thinking {
    color: hsl(var(--muted-foreground, 220 9% 46%));
    font-style: italic;
}
.mod-asst-ai-badge {
    color: hsl(var(--muted-foreground, 220 9% 46%));
    font-size: 10px;
    opacity: .6;
}
.mod-asst-actions { display: flex; flex-direction: column; gap: 6px; margin-top: 6px; }
.mod-asst-action-btn {
    background: hsl(var(--accent, 232 96% 42%));
    color: hsl(var(--accent-foreground, 0 0% 100%));
    border: 0; padding: 6px 10px; border-radius: 8px;
    font-size: 12px; font-weight: 500; cursor: pointer;
    text-align: left;
}
.mod-asst-action-btn:hover { filter: brightness(1.08); }
.mod-asst-citations { display: flex; flex-direction: column; gap: 4px; margin-top: 6px; }
.mod-asst-citation {
    font-size: 11px;
    color: hsl(var(--muted-foreground, 220 9% 46%));
    text-decoration: underline;
}
.mod-asst-citation:hover { color: hsl(var(--foreground, 0 0% 12%)); }
.mod-asst-empty {
    display: flex; flex-direction: column; gap: 8px;
    padding: 8px; align-items: flex-start;
}
.mod-asst-empty-headline {
    font-size: 13px; line-height: 1.5;
    color: hsl(var(--muted-foreground, 220 9% 46%));
    margin: 0;
}
.mod-asst-suggested {
    background: hsl(var(--muted, 220 13% 96%));
    color: hsl(var(--foreground, 0 0% 12%));
    border: 1px solid hsl(var(--border, 220 13% 91%));
    border-radius: 999px; padding: 6px 12px; font-size: 12px;
    cursor: pointer;
}
.mod-asst-suggested:hover { background: hsl(var(--accent, 232 96% 42%) / 0.1); }
.mod-asst-jump-pill {
    position: absolute; bottom: 76px; left: 50%;
    transform: translateX(-50%);
    background: hsl(var(--primary, 232 96% 42%));
    color: hsl(var(--primary-foreground, 0 0% 100%));
    border: 0; border-radius: 999px; padding: 4px 12px;
    font-size: 11px; cursor: pointer;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
}
.mod-asst-jump-pill.hidden { display: none; }
.mod-asst-form {
    display: flex; gap: 8px; padding: 12px 16px;
    border-top: 1px solid hsl(var(--border, 220 13% 91%));
    flex-shrink: 0;
}
.mod-asst-input {
    flex: 1; padding: 8px 12px;
    border: 1px solid hsl(var(--border, 220 13% 91%));
    border-radius: 8px; font-size: 13px;
    background: hsl(var(--background, 0 0% 100%));
    color: inherit;
}
.mod-asst-input:focus {
    outline: 2px solid hsl(var(--primary, 232 96% 42%));
    outline-offset: -1px;
}
.mod-asst-send {
    background: hsl(var(--primary, 232 96% 42%));
    color: hsl(var(--primary-foreground, 0 0% 100%));
    border: 0; padding: 8px 14px; border-radius: 8px;
    font-size: 13px; font-weight: 500; cursor: pointer;
}
.mod-asst-send:hover { filter: brightness(1.08); }
.mod-asst-send:disabled { opacity: 0.6; cursor: not-allowed; }
.mod-asst-foot {
    display: flex; justify-content: center; padding: 6px 12px 10px;
    flex-shrink: 0;
}
.mod-asst-foot-link {
    background: none; border: 0; padding: 4px 8px;
    font-size: 12px; color: hsl(var(--muted-foreground, 220 9% 46%));
    cursor: pointer; text-decoration: underline;
}
.mod-asst-foot-link:hover { color: hsl(var(--primary, 232 96% 42%)); }
.mod-asst-escalate-form {
    display: flex; flex-direction: column; gap: 8px;
    padding: 14px; margin: 0 16px;
    background: hsl(var(--muted, 220 13% 96%));
    border: 1px solid hsl(var(--border, 220 13% 91%));
    border-radius: 10px;
}
.mod-asst-escalate-title {
    font-size: 13px; font-weight: 600; margin-bottom: 2px;
}
.mod-asst-escalate-input,
.mod-asst-escalate-textarea,
.mod-asst-escalate-select {
    width: 100%; padding: 7px 10px; font-size: 13px;
    border: 1px solid hsl(var(--border, 220 13% 91%));
    border-radius: 6px;
    background: hsl(var(--background, 0 0% 100%));
    color: inherit;
    font-family: inherit;
}
.mod-asst-escalate-textarea { resize: vertical; min-height: 60px; }
.mod-asst-escalate-row {
    display: flex; align-items: center; gap: 8px;
}
.mod-asst-escalate-sev-label {
    font-size: 12px; color: hsl(var(--muted-foreground, 220 9% 46%));
}
.mod-asst-support-receipt {
    display: flex; gap: 10px; align-items: flex-start;
    padding: 8px 12px;
    background: hsl(var(--primary, 232 96% 42%) / 0.06);
    border: 1px solid hsl(var(--primary, 232 96% 42%) / 0.18);
    border-radius: 8px;
    font-size: 12px;
}
.mod-asst-support-receipt-icon {
    color: hsl(var(--primary, 232 96% 42%));
    font-weight: 700; line-height: 1.2;
}
.mod-asst-support-receipt-body { display: flex; flex-direction: column; gap: 2px; }
.mod-asst-support-receipt-headline { font-weight: 600; color: #1f2937; }
.mod-asst-support-receipt-detail,
.mod-asst-support-receipt-subject {
    color: hsl(var(--muted-foreground, 220 9% 46%));
}
/* Confirmation dialog (role=dialog + aria-modal on the overlay). */
.mod-asst-confirm-dialog {
    position: fixed; inset: 0; z-index: 70;
    display: flex; align-items: center; justify-content: center;
    background: rgba(0,0,0,0.45);
}
.mod-asst-confirm-dialog > * {
    max-width: 620px; margin: auto;
    background: hsl(var(--card, 0 0% 100%));
    color: hsl(var(--card-foreground, 0 0% 12%));
    border: 1px solid hsl(var(--border, 220 13% 91%));
    border-radius: 12px;
    box-shadow: 0 8px 24px 8px rgba(0,0,0,0.18);
    padding: 16px 18px;
    flex-direction: column;
}
.mod-asst-confirm-title { font-weight: 600; font-size: 14px; }
.mod-asst-confirm-note { font-size: 12px; color: hsl(var(--muted-foreground, 220 9% 46%)); }
.mod-asst-confirm-row { flex-direction: column; gap: 2px; }
.mod-asst-confirm-tool { font-size: 12px; font-weight: 600; }
.mod-asst-confirm-summary,
.mod-asst-confirm-forced {
    font-size: 11px; color: hsl(var(--muted-foreground, 220 9% 46%));
}
`;

    function _injectCss() {
        if (document.getElementById("mod-asst-css")) return;
        const styleEl = document.createElement("style");
        styleEl.id = "mod-asst-css";
        styleEl.textContent = CSS;
        document.head.appendChild(styleEl);
    }

    // ---------- gating + bootstrap ----------------------------------------

    // 3-way result: config object on success, "no-token" if the
    // bearer wasn't ready (retryable), or null on HTTP/network error
    // (also retryable, because the API might just be mid-restart).
    // Matches the launcher's _fetchConfig contract; both scripts have
    // to dance the same auth-race because they both ship as plain
    // <script defer> tags that fire on DOMContentLoaded BEFORE
    // main.js's exchangeCodeForToken resolves.
    async function _fetchConfig() {
        var token;
        try { token = sessionStorage.getItem("accessToken"); }
        catch (_) { token = null; }
        if (!token) return "no-token";
        try {
            const apiBase = (window.APP_CONFIG && window.APP_CONFIG.API_URL) || "";
            const resp = await fetch(`${apiBase}/assistant/config`, {
                headers: { Authorization: `Bearer ${token}` },
            });
            if (!resp || !resp.ok) return null;
            return await resp.json();
        } catch (_) { return null; }
    }

    // Retry budget. Pre-fix this script ran exactly once on
    // DOMContentLoaded; if the auth race fired before
    // exchangeCodeForToken resolved, the panel DOM was never built
    // AND the launcher silently failed too. With the launcher race
    // now fixed (in assistant-launcher.js), the button would appear
    // but clicking it did nothing because this script also gave up.
    // Same 12 × 250ms ≈ 3s budget keeps both halves of the chrome
    // consistent.
    var MAX_INIT_ATTEMPTS = 12;
    var INIT_RETRY_MS = 250;
    var _initInFlight = false;
    var _initDone = false;

    // 2400UI17 — CE carve (see assistant-launcher.js for the rationale): this
    // script is a classic <script defer> tag (index.html:74), so dynamic
    // import() resolves '/js/api.js' against the document URL and yields the
    // SAME api.js module instance the rest of the Dashboard already loaded —
    // i.e. the same coalesced 30s /platform/features cache. Gating _init skips
    // BOTH /assistant/config and the /assistant/history resume that hangs off
    // it; with no panel DOM, /assistant/chat and /assistant/escalate are
    // unreachable too. Fail-open: unreadable payload / import failure = not CE.
    async function _ceEdition() {
        try {
            const mod = await import('/js/api.js');
            return !(await mod.isPaidEdition());
        } catch (_) { return false; }
    }

    async function _init() {
        if (_initDone || _initInFlight) return;
        _initInFlight = true;
        try {
            for (var attempt = 0; attempt < MAX_INIT_ATTEMPTS; attempt++) {
                var cfg = await _fetchConfig();
                if (cfg && typeof cfg === "object") {
                    if (cfg.enabled === false) {
                        _initDone = true;
                        return;
                    }
                    _injectCss();
                    panelEl = _buildPanel();
                    document.body.appendChild(panelEl);
                    // FIX-THEME-GLINDA: the panel mounts after ui.js dispatched
                    // the page-rendered event, so the label hook never saw the
                    // title span. Re-dispatch (same event ui.js:1015 uses; the
                    // applier is idempotent and an OFF no-op).
                    try { document.dispatchEvent(new CustomEvent('mod:page-rendered')); } catch (_ozEvErr) { /* engines without CustomEvent */ }
                    _renderEmptyState();
                    // Resume a recent conversation (cross-navigation/surface
                    // continuity). Repaints over the empty state if found.
                    await _resumeHistory();

                    if (document.body.classList.contains(BODY_CLASS)) {
                        inputEl.focus();
                    }
                    var obs = new MutationObserver(function () {
                        if (document.body.classList.contains(BODY_CLASS) && inputEl) {
                            inputEl.focus();
                        }
                    });
                    obs.observe(document.body, { attributes: true, attributeFilter: ["class"] });

                    var pending = _readAndClearPendingActions();
                    if (pending.length > 0) {
                        setTimeout(function () {
                            pending.forEach(function (a) { _executeAction(a); });
                        }, 300);
                    }
                    _initDone = true;
                    return;
                }
                // "no-token" or null — both retryable.
                await new Promise(function (r) { setTimeout(r, INIT_RETRY_MS); });
            }
            console.warn(
                "[assistant-panel] init failed after " + MAX_INIT_ATTEMPTS +
                " attempts; waiting for token-arrival event.",
            );
        } finally {
            _initInFlight = false;
        }
    }

    // Fast-path wakeups for the auth race, mirroring the launcher.
    // Without these the panel would have to wait the full 3s budget
    // before a late-arriving token wakes it up; the events fire as
    // soon as the token lands so the panel mounts immediately.
    window.addEventListener("mod:auth:token-refreshed", function () {
        if (!_initDone) _init();
    });
    window.addEventListener("storage", function (e) {
        // Best-effort cross-tab wake. Dashboard tokens are in sessionStorage
        // (GSEC R-077) and never fire a 'storage' event in a sibling tab, so
        // this only catches a token mirrored into localStorage. Watch both
        // token keys (restored to the pre-R-077 set) so no variant is missed;
        // the reliable same-tab signal is mod:auth:token-refreshed above.
        if (!_initDone && e && (e.key === "accessToken" || e.key === "workflow_access_token")) {
            _init();
        }
    });

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", _init);
    } else {
        _init();
    }
})();
