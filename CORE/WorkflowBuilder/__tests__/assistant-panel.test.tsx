/**
 * Tests for the AssistantPanel component (mod-assistant-agent §8).
 *
 * Pure UI behaviour — gate, body-class watch, empty state, message
 * rendering, action button rendering. The SSE stream itself is
 * mocked via fetch + a minimal ReadableStream so we don't depend
 * on a real backend.
 */

import React from "react";
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react";

// jsdom doesn't expose ReadableStream / TextEncoder / TextDecoder
// globally; pull the Node implementations so the SSE mock can build
// a real stream. Must run before the component import so its
// fetch-handling path sees the globals.
import { ReadableStream as NodeReadableStream } from "node:stream/web";
import { TextEncoder as NodeTextEncoder, TextDecoder as NodeTextDecoder } from "node:util";
if (typeof (globalThis as any).ReadableStream === "undefined") {
    (globalThis as any).ReadableStream = NodeReadableStream;
}
if (typeof (globalThis as any).TextEncoder === "undefined") {
    (globalThis as any).TextEncoder = NodeTextEncoder;
}
if (typeof (globalThis as any).TextDecoder === "undefined") {
    (globalThis as any).TextDecoder = NodeTextDecoder;
}

import AssistantPanel from "@/components/assistant-panel";
// 2400UI76 — the CE case below resolves a real /platform/features block; the
// module-level cache in lib/platform-features would leak it into the tests
// after it (fail-open paid behaviour), so tear it down explicitly.
import { _resetPlatformFeaturesCache } from "@/lib/platform-features";

// Helper: build a fetch mock that returns the given /assistant/config
// JSON, then an SSE stream for /assistant/chat composed of the given
// raw event blocks.
function mockFetchSequence(
    cfgEnabled: boolean,
    chatBlocks: string[],
) {
    const blocks = chatBlocks.map(b => new TextEncoder().encode(b));
    const stream = new ReadableStream({
        start(controller) {
            for (const chunk of blocks) controller.enqueue(chunk);
            controller.close();
        },
    });
    return jest.fn((url: string) => {
        if (url.endsWith("/assistant/config")) {
            return Promise.resolve({
                ok: true,
                json: async () => ({ enabled: cfgEnabled }),
            } as any);
        }
        if (url.endsWith("/assistant/chat")) {
            return Promise.resolve({ ok: true, body: stream } as any);
        }
        return Promise.resolve({ ok: false, status: 404 } as any);
    });
}

function setBodyOpen(open: boolean) {
    if (open) document.body.classList.add("assistant-panel-open");
    else document.body.classList.remove("assistant-panel-open");
}

describe("AssistantPanel", () => {
    beforeEach(() => {
        document.body.className = "";
        try { sessionStorage.clear(); localStorage.clear(); } catch { /* noop */ }
        _resetPlatformFeaturesCache();
    });

    it("renders nothing when no access token (gate fails)", () => {
        const { container } = render(
            <AssistantPanel accessToken={null} apiUrl="http://api.test" />,
        );
        expect(container.firstChild).toBeNull();
    });

    it("renders nothing when the tenant config says disabled", async () => {
        global.fetch = mockFetchSequence(false, []) as any;
        const { container } = render(
            <AssistantPanel accessToken="t" apiUrl="http://api.test" />,
        );
        await waitFor(() => {
            // After the gate resolves, still null.
            expect(container.firstChild).toBeNull();
        });
    });

    it("renders the empty state with suggested prompts when enabled", async () => {
        global.fetch = mockFetchSequence(true, []) as any;
        render(<AssistantPanel accessToken="t" apiUrl="http://api.test" />);
        // Panel-rendered proxy: the input shows up only when the
        // gate resolves enabled=true. The header text "MOD Assistant"
        // is split into nodes by the Bot icon and is brittle to
        // match.
        await screen.findByPlaceholderText(/Ask me anything/i);
        // Three of the suggested prompts should render as buttons
        expect(screen.getByText("Create a new node")).toBeInTheDocument();
        expect(screen.getByText("Show me my usage")).toBeInTheDocument();
        expect(screen.getByText("Show me my workers")).toBeInTheDocument();
    });

    it("streams tokens into the assistant bubble and renders an action button", async () => {
        const events = [
            'event: text-token\ndata: {"text":"Hi "}\n\n',
            'event: text-token\ndata: {"text":"there"}\n\n',
            'event: action-emit\ndata: {"action":"navigate","url":"/billing"}\n\n',
            'event: done\ndata: {}\n\n',
        ];
        global.fetch = mockFetchSequence(true, events) as any;
        render(<AssistantPanel accessToken="t" apiUrl="http://api.test" />);
        // Panel-rendered proxy: the input shows up only when the
        // gate resolves enabled=true. The header text "MOD Assistant"
        // is split into nodes by the Bot icon and is brittle to
        // match.
        await screen.findByPlaceholderText(/Ask me anything/i);

        // Submit a message by typing + form submit
        const input = screen.getByPlaceholderText(/Ask me anything/i) as HTMLInputElement;
        await act(async () => {
            fireEvent.change(input, { target: { value: "hello" } });
            fireEvent.submit(input.closest("form")!);
        });

        // User bubble appears
        await screen.findByText("hello");
        // Assistant text streams in
        await waitFor(() => expect(screen.getByText(/Hi there/)).toBeInTheDocument());
        // Action button renders with the right label
        await screen.findByText("Take me to /billing");
    });

    it("prefers the server-supplied label so two same-app navigates don't render identically", async () => {
        // Regression: pre-fix the panel labelled every navigate by app
        // only ("Take me to the Dashboard"), so a reply that offered
        // both Billing and Workers rendered two buttons with identical
        // copy. The server now decorates navigates with the destination
        // page's display_name; the panel must surface it verbatim.
        const events = [
            'event: text-token\ndata: {"text":"You can go to either."}\n\n',
            'event: action-emit\ndata: {"action":"navigate","url":"/#billing","app":"dashboard","label":"Billing"}\n\n',
            'event: action-emit\ndata: {"action":"navigate","url":"/#workers","app":"dashboard","label":"Workers"}\n\n',
            'event: done\ndata: {}\n\n',
        ];
        global.fetch = mockFetchSequence(true, events) as any;
        render(<AssistantPanel accessToken="t" apiUrl="http://api.test" />);
        await screen.findByPlaceholderText(/Ask me anything/i);

        const input = screen.getByPlaceholderText(/Ask me anything/i) as HTMLInputElement;
        await act(async () => {
            fireEvent.change(input, { target: { value: "where can I go" } });
            fireEvent.submit(input.closest("form")!);
        });

        await screen.findByText("Take me to Billing");
        await screen.findByText("Take me to Workers");
        // And the buttons are distinct — the bug would have produced
        // two identical "Take me to the Dashboard" buttons.
        expect(screen.queryByText("Take me to Dashboard")).toBeNull();
        expect(screen.queryByText("Take me to the Dashboard")).toBeNull();
    });

    it("renders an error row and Retry when the chat request fails", async () => {
        global.fetch = jest.fn((url: string) => {
            if (url.endsWith("/assistant/config")) {
                return Promise.resolve({ ok: true, json: async () => ({ enabled: true }) } as any);
            }
            return Promise.resolve({ ok: false, status: 500, body: null } as any);
        }) as any;

        render(<AssistantPanel accessToken="t" apiUrl="http://api.test" />);
        // Panel-rendered proxy: the input shows up only when the
        // gate resolves enabled=true. The header text "MOD Assistant"
        // is split into nodes by the Bot icon and is brittle to
        // match.
        await screen.findByPlaceholderText(/Ask me anything/i);

        const input = screen.getByPlaceholderText(/Ask me anything/i) as HTMLInputElement;
        await act(async () => {
            fireEvent.change(input, { target: { value: "anything" } });
            fireEvent.submit(input.closest("form")!);
        });

        await screen.findByText(/Network or server error/i);
        await screen.findByText("Retry");
    });

    it("close button removes the body class", async () => {
        global.fetch = mockFetchSequence(true, []) as any;
        render(<AssistantPanel accessToken="t" apiUrl="http://api.test" />);
        // Panel-rendered proxy: the input shows up only when the
        // gate resolves enabled=true. The header text "MOD Assistant"
        // is split into nodes by the Bot icon and is brittle to
        // match.
        await screen.findByPlaceholderText(/Ask me anything/i);
        setBodyOpen(true);

        const closeBtn = screen.getByLabelText("Close");
        fireEvent.click(closeBtn);
        expect(document.body.classList.contains("assistant-panel-open")).toBe(false);
    });

    it("renders the support-contacted action as a receipt, not a clickable button", async () => {
        // Audit-finding consolidation (Option A): when the orchestrator
        // emits a support-contacted action, the panel must show it as
        // an informational row (no onClick, no button styling). A
        // forgetful model or a UI change that treated it like a
        // navigate would tell users to "click" something that doesn't
        // navigate anywhere.
        const events = [
            'event: text-token\ndata: {"text":"I\'ve contacted support."}\n\n',
            'event: action-emit\ndata: {"action":"support-contacted","recipient":"admin@example.com","severity":"emergency","tenant_admin_count":2,"subject":"Workers offline"}\n\n',
            'event: done\ndata: {}\n\n',
        ];
        global.fetch = mockFetchSequence(true, events) as any;
        render(<AssistantPanel accessToken="t" apiUrl="http://api.test" />);
        await screen.findByPlaceholderText(/Ask me anything/i);

        const input = screen.getByPlaceholderText(/Ask me anything/i) as HTMLInputElement;
        await act(async () => {
            fireEvent.change(input, { target: { value: "i need a human" } });
            fireEvent.submit(input.closest("form")!);
        });

        // Receipt copy lands.
        await screen.findByText(/Support contacted — flagged emergency/);
        await screen.findByText(/Transcript emailed to admin@example\.com/);
        await screen.findByText(/cc'd 2 tenant admins/);
        await screen.findByText(/Subject: Workers offline/);

        // Not rendered as a clickable button — `Support contacted` is
        // inside a `div`, not a `button`, so role-based query for the
        // headline text shouldn't match a button element.
        const buttons = screen.queryAllByRole("button");
        const buttonTexts = buttons.map((b) => b.textContent || "");
        expect(buttonTexts.some((t) => t.includes("Support contacted"))).toBe(false);
    });

    // FIX-CE-SHOW-ASSISTANT (owner 11-10 "Show them in CE"): the former 2400UI16
    // CE hide is GONE — the /assistant/* API ships in CE (open-core/manifest.yaml
    // deny_exceptions), so CE now behaves EXACTLY like paid: the panel renders,
    // GET /assistant/config is issued on mount (assistant-panel.tsx:299 fetchOnce)
    // and the tab-focus re-check re-issues it (assistant-panel.tsx:331 focus
    // listener). The old "no /assistant/* request in CE" contract is stale.
    it("CE edition: fetches /assistant/config on mount and on focus (assistant shown in CE)", async () => {
        const seen: string[] = [];
        global.fetch = jest.fn((url: string) => {
            seen.push(url);
            if (url.endsWith("/platform/features")) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ entitlements: { edition: "ce", capabilities: [] } }),
                } as any);
            }
            if (url.endsWith("/assistant/config")) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ enabled: true }),
                } as any);
            }
            return Promise.resolve({ ok: false, status: 404 } as any);
        }) as any;

        render(<AssistantPanel accessToken="t" apiUrl="http://api.test" />);
        // CE shows the panel now — same render contract as paid.
        await screen.findByPlaceholderText(/Ask me anything/i);
        await new Promise((r) => setTimeout(r, 50));
        // Mount: exactly one config fetch (a definitive ok stops the retry loop)
        // and the one-shot history resume (historyLoadedRef guard).
        expect(seen.filter((u) => u.endsWith("/assistant/config"))).toHaveLength(1);
        expect(seen.filter((u) => u.endsWith("/assistant/history"))).toHaveLength(1);

        // The focus re-check re-issues GET /assistant/config on every focus
        // event (paid behaviour; no CE guard any more).
        const before = seen.filter((u) => u.endsWith("/assistant/config")).length;
        act(() => {
            window.dispatchEvent(new Event("focus"));
            window.dispatchEvent(new Event("focus"));
        });
        await new Promise((r) => setTimeout(r, 50));
        expect(seen.filter((u) => u.endsWith("/assistant/config"))).toHaveLength(before + 2);
        // history stays one-shot per mount — focus never re-fetches it.
        expect(seen.filter((u) => u.endsWith("/assistant/history"))).toHaveLength(1);
    });

    it("footer 'Contact human support' opens the escalate form", async () => {
        global.fetch = mockFetchSequence(true, []) as any;
        render(<AssistantPanel accessToken="t" apiUrl="http://api.test" />);
        await screen.findByPlaceholderText(/Ask me anything/i);

        const link = screen.getByText("Contact human support");
        fireEvent.click(link);

        // Subject + textarea + send button appear.
        await screen.findByPlaceholderText(/Short subject/);
        await screen.findByPlaceholderText(/What's happening/);
        await screen.findByText("Send to support");
    });
});
