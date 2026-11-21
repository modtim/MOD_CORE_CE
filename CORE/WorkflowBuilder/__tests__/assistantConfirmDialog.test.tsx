/**
 * Assistant UI confirmation dialog (WB panel).
 *
 * The orchestrator refuses a risky tool call and answers with a gate
 * envelope (CORE/API/services/assistant_confirm.py:check_tool_call ->
 * {needs_confirmation, confirm_token, summary, risk_category, forced_by});
 * the client must show an accessible dialog and, on Confirm, re-POST the
 * SAME request with `confirm_tokens` (CORE/API/services/assistant_orchestrator.py:1858).
 *
 * The SSE stream itself is mocked. Asserted:
 *   (a) confirm-required -> dialog renders with the pending tool + summary;
 *   (b) Confirm -> exactly one more /assistant/chat POST, carrying
 *       confirm_tokens, and the token is never sent twice;
 *   (c) Cancel / Escape -> NO further POST, "Action cancelled" in the chat;
 *   (d) tool args are escaped (hostile arg text never becomes markup);
 *   (e) expired/invalid-token error -> shown verbatim, no Retry offered;
 *   (f) a11y attributes: role=dialog, aria-modal, aria-labelledby, tabindex.
 */

import React from "react";
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react";

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

// Build a fetch mock: /assistant/config -> enabled, then one SSE stream per
// /assistant/chat POST (in order), so a Confirm re-send gets its own stream.
// Every chat POST's JSON body is recorded in `chatBodies` so the tests can
// assert exactly what was sent (and that Cancel sends nothing).
function mockFetchChatStreams(chatBlocks: string[][], chatBodies: any[]) {
    const streams = chatBlocks.map((blocks) => {
        const chunks = blocks.map((b) => new TextEncoder().encode(b));
        return new NodeReadableStream({
            start(controller) {
                for (const c of chunks) controller.enqueue(c);
                controller.close();
            },
        });
    });
    let chatIndex = 0;
    return jest.fn((url: string, opts: any) => {
        if (url.endsWith("/assistant/config")) {
            return Promise.resolve({ ok: true, json: async () => ({ enabled: true }) } as any);
        }
        if (url.endsWith("/assistant/chat")) {
            chatBodies.push(JSON.parse(String(opts?.body ?? "{}")));
            const body = streams[Math.min(chatIndex++, streams.length - 1)];
            return Promise.resolve({ ok: true, body } as any);
        }
        return Promise.resolve({ ok: false, status: 404 } as any);
    });
}

const CONFIRM_FIRST = [
    'event: text-token\ndata: {"text":"I want to delete a workflow."}\n\n',
    'event: needs-confirmation\ndata: {"name":"delete_workflow","args":{"workflow_id":42},'
    + '"needs_confirmation":true,"confirm_token":"tok-abc-123",'
    + '"summary":"assistant wants to run delete_workflow (delete): workflow_id=42",'
    + '"risk_category":"delete"}\n\n',
    'event: done\ndata: {}\n\n',
];

const CONFIRM_SECOND = [
    'event: text-token\ndata: {"text":"Deleted."}\n\n',
    'event: done\ndata: {}\n\n',
];

const HOSTILE = [
    'event: needs-confirmation\ndata: {"name":"delete_workflow","args":{'
    + '"name":"<img src=x onerror=alert(1)>"}, "needs_confirmation":true,'
    + '"confirm_token":"tok-2"}\n\n',
    'event: done\ndata: {}\n\n',
];

const UNTRUSTED = [
    'event: needs-confirmation\ndata: {"name":"delete_workflow","args":{"workflow_id":7},'
    + '"needs_confirmation":true,"confirm_token":"tok-3","forced_by":"untrusted_content",'
    + '"risk_category":"untrusted_context"}\n\n',
    'event: done\ndata: {}\n\n',
];

const EXPIRED = [
    'event: error\ndata: {"message":"Confirm token already used or expired."}\n\n',
    'event: done\ndata: {}\n\n',
];

function setBodyOpen(open: boolean) {
    if (open) document.body.classList.add("assistant-panel-open");
}

describe("AssistantPanel confirm dialog", () => {
    beforeEach(() => {
        document.body.className = "";
        try { sessionStorage.clear(); localStorage.clear(); } catch { /* noop */ }
    });

    it("confirm-required turn opens the dialog with the pending tool + summary", async () => {
        const bodies: any[] = [];
        global.fetch = mockFetchChatStreams([CONFIRM_FIRST, CONFIRM_SECOND], bodies) as any;
        render(<AssistantPanel accessToken="t" apiUrl="http://api.test" />);
        await screen.findByPlaceholderText(/Ask me anything/i);
        setBodyOpen(true);

        const input = screen.getByPlaceholderText(/Ask me anything/i) as HTMLInputElement;
        await act(async () => {
            fireEvent.change(input, { target: { value: "delete workflow 42" } });
            fireEvent.submit(input.closest("form")!);
        });

        // The dialog is announced.
        const dialog = screen.getByRole("dialog");
        expect(dialog).toBeInTheDocument();
        // The pending tool name and the gate's own summary are shown.
        await screen.findByText("delete_workflow");
        await screen.findByText(/assistant wants to run delete_workflow/);
        // Confirm + Cancel controls exist.
        await screen.findByRole("button", { name: "Confirm" });
        await screen.findByRole("button", { name: "Cancel" });
    });

    it("Confirm re-sends the same request with confirm_tokens (token once only)", async () => {
        const bodies: any[] = [];
        global.fetch = mockFetchChatStreams([CONFIRM_FIRST, CONFIRM_SECOND], bodies) as any;
        render(<AssistantPanel accessToken="t" apiUrl="http://api.test" />);
        await screen.findByPlaceholderText(/Ask me anything/i);
        setBodyOpen(true);

        const input = screen.getByPlaceholderText(/Ask me anything/i) as HTMLInputElement;
        await act(async () => {
            fireEvent.change(input, { target: { value: "delete workflow 42" } });
            fireEvent.submit(input.closest("form")!);
        });
        await screen.findByRole("dialog");

        const confirmBtn = screen.getByRole("button", { name: "Confirm" });
        await act(async () => {
            fireEvent.click(confirmBtn);
        });

        // Two POSTs: the original turn and the confirmed re-send.
        await waitFor(() => expect(bodies.length).toBe(2));
        expect(bodies[0].message).toBe("delete workflow 42");
        expect(bodies[0].confirm_tokens).toBeUndefined();
        expect(bodies[1].message).toBe("delete workflow 42");
        expect(bodies[1].confirm_tokens).toEqual({ delete_workflow: "tok-abc-123" });
        // The re-send is the SAME request shape (session + app unchanged).
        expect(bodies[1].current_app).toBe("workflow_builder");
        // Dialog is gone after Confirm.
        expect(screen.queryByRole("dialog")).toBeNull();
        // And no third POST ever happens (token is single-use).
        await new Promise((r) => setTimeout(r, 50));
        expect(bodies.length).toBe(2);
    }, 20000);

    it("Cancel sends nothing and shows 'Action cancelled'", async () => {
        const bodies: any[] = [];
        global.fetch = mockFetchChatStreams([CONFIRM_FIRST], bodies) as any;
        render(<AssistantPanel accessToken="t" apiUrl="http://api.test" />);
        await screen.findByPlaceholderText(/Ask me anything/i);
        setBodyOpen(true);

        const input = screen.getByPlaceholderText(/Ask me anything/i) as HTMLInputElement;
        await act(async () => {
            fireEvent.change(input, { target: { value: "delete workflow 42" } });
            fireEvent.submit(input.closest("form")!);
        });
        await screen.findByRole("dialog");

        const cancelBtn = screen.getByRole("button", { name: "Cancel" });
        fireEvent.click(cancelBtn);

        // Only the original POST — Cancel fires no request at all.
        expect(bodies.length).toBe(1);
        await screen.findByText(/^Action cancelled$/);
        expect(screen.queryByRole("dialog")).toBeNull();
    });

    it("Escape closes the dialog as Cancel: no request, 'Action cancelled'", async () => {
        const bodies: any[] = [];
        global.fetch = mockFetchChatStreams([CONFIRM_FIRST], bodies) as any;
        render(<AssistantPanel accessToken="t" apiUrl="http://api.test" />);
        await screen.findByPlaceholderText(/Ask me anything/i);
        setBodyOpen(true);

        const input = screen.getByPlaceholderText(/Ask me anything/i) as HTMLInputElement;
        await act(async () => {
            fireEvent.change(input, { target: { value: "delete workflow 42" } });
            fireEvent.submit(input.closest("form")!);
        });
        await screen.findByRole("dialog");

        await act(async () => {
            document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
        });

        expect(bodies.length).toBe(1);
        await screen.findByText(/^Action cancelled$/);
        expect(screen.queryByRole("dialog")).toBeNull();
    });

    it("hostile tool args render as text, never markup", async () => {
        const bodies: any[] = [];
        global.fetch = mockFetchChatStreams([HOSTILE], bodies) as any;
        render(<AssistantPanel accessToken="t" apiUrl="http://api.test" />);
        await screen.findByPlaceholderText(/Ask me anything/i);
        setBodyOpen(true);

        const input = screen.getByPlaceholderText(/Ask me anything/i) as HTMLInputElement;
        await act(async () => {
            fireEvent.change(input, { target: { value: "delete it" } });
            fireEvent.submit(input.closest("form")!);
        });
        await screen.findByRole("dialog");

        // The hostile string is visible text (as part of the arg summary)...
        await screen.findByText(/<img src=x onerror=alert\(1\)>/);
        // ...and no element was created from it.
        const dialog = screen.getByRole("dialog");
        expect(dialog.querySelectorAll("img")).toHaveLength(0);
        expect(dialog.querySelectorAll("[onerror]")).toHaveLength(0);
    });

    it("untrusted-content turn says so in the dialog", async () => {
        const bodies: any[] = [];
        global.fetch = mockFetchChatStreams([UNTRUSTED], bodies) as any;
        render(<AssistantPanel accessToken="t" apiUrl="http://api.test" />);
        await screen.findByPlaceholderText(/Ask me anything/i);
        setBodyOpen(true);

        const input = screen.getByPlaceholderText(/Ask me anything/i) as HTMLInputElement;
        await act(async () => {
            fireEvent.change(input, { target: { value: "delete it" } });
            fireEvent.submit(input.closest("form")!);
        });
        await screen.findByRole("dialog");

        await screen.findByText(/content from outside MOD/);
        await screen.findByText(/Forced by untrusted content/);
    });

    it("expired/invalid confirm token: error shown, nothing offered automatic", async () => {
        const bodies: any[] = [];
        global.fetch = mockFetchChatStreams([EXPIRED], bodies) as any;
        render(<AssistantPanel accessToken="t" apiUrl="http://api.test" />);
        await screen.findByPlaceholderText(/Ask me anything/i);
        setBodyOpen(true);

        const input = screen.getByPlaceholderText(/Ask me anything/i) as HTMLInputElement;
        await act(async () => {
            fireEvent.change(input, { target: { value: "delete it" } });
            fireEvent.submit(input.closest("form")!);
        });

        await screen.findByText(/Confirm token already used or expired\./);
        // No Retry button next to it: retrying would re-run without a token.
        const buttons = screen.queryAllByRole("button");
        expect(buttons.map((b) => b.textContent || "").some((t) => /Retry/.test(t))).toBe(false);
        // And no dialog is offered either — the client cannot re-mint a token.
        expect(screen.queryByRole("dialog")).toBeNull();
    });

    it("dialog carries the a11y attributes", async () => {
        const bodies: any[] = [];
        global.fetch = mockFetchChatStreams([CONFIRM_FIRST], bodies) as any;
        render(<AssistantPanel accessToken="t" apiUrl="http://api.test" />);
        await screen.findByPlaceholderText(/Ask me anything/i);
        setBodyOpen(true);

        const input = screen.getByPlaceholderText(/Ask me anything/i) as HTMLInputElement;
        await act(async () => {
            fireEvent.change(input, { target: { value: "delete workflow 42" } });
            fireEvent.submit(input.closest("form")!);
        });

        const dialog = screen.getByRole("dialog");
        expect(dialog.getAttribute("aria-modal")).toBe("true");
        expect(dialog.getAttribute("aria-labelledby")).toBe("mod-asst-confirm-title");
        expect(dialog.getAttribute("tabindex")).toBe("-1");
        expect(dialog.getAttribute("id")).toBe("mod-assistant-confirm-dialog");
        // The label target exists and is announced.
        await screen.findByText("Confirm before the assistant acts");
    });
});
