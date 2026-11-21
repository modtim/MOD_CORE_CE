/**
 * Task 667 (WSIP 4.5b) — the tenant `conversational_debug_enabled` flag gates
 * RunRail's "Why did this happen?" button (workflow-builder.tsx passes
 * onExplainRun={debugEnabled ? handleExplainRun : undefined}).
 *
 * Drives the hook directly (workflow-builder.tsx is too heavy to render here):
 *   true -> true; 404 (router unmounted) -> false; fetch reject -> false;
 *   non-boolean field -> false; null token -> NO fetch at all.
 * Everything is FAIL CLOSED: false is the default and the error path.
 */

import { renderHook, waitFor } from "@testing-library/react";
import { useConversationalDebugFlag } from "@/hooks/use-conversational-debug-flag";

const API = "https://mod.example/api/v1";

function makeResponse(status: number, body: unknown = {}): any {
    return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => body,
    };
}

const respond = (status: number, body: unknown) => {
    global.fetch = jest.fn(() => Promise.resolve(makeResponse(status, body))) as any;
};
const rejectFetch = () => {
    global.fetch = jest.fn(() => Promise.reject(new Error("offline"))) as any;
};

afterEach(() => {
    global.fetch = undefined as any;
    jest.clearAllMocks();
});

describe("useConversationalDebugFlag", () => {
    it("flag true -> hook returns true (button shown)", async () => {
        respond(200, { conversational_debug_enabled: true });
        const { result } = renderHook(() => useConversationalDebugFlag(API, "tok"));
        await waitFor(() => expect(result.current).toBe(true));
    });

    it("flag false -> hook returns false (button hidden)", async () => {
        respond(200, { conversational_debug_enabled: false });
        const { result } = renderHook(() => useConversationalDebugFlag(API, "tok"));
        await waitFor(() => expect(result.current).toBe(false));
    });

    it("404 while the router is unmounted -> false", async () => {
        respond(404, { detail: "Not Found" });
        const { result } = renderHook(() => useConversationalDebugFlag(API, "tok"));
        await waitFor(() => expect(result.current).toBe(false));
    });

    it("401 -> false", async () => {
        respond(401, {});
        const { result } = renderHook(() => useConversationalDebugFlag(API, "tok"));
        await waitFor(() => expect(result.current).toBe(false));
    });

    it("5xx -> false", async () => {
        respond(502, {});
        const { result } = renderHook(() => useConversationalDebugFlag(API, "tok"));
        await waitFor(() => expect(result.current).toBe(false));
    });

    it("network error -> false", async () => {
        rejectFetch();
        const { result } = renderHook(() => useConversationalDebugFlag(API, "tok"));
        await waitFor(() => expect(result.current).toBe(false));
    });

    it("non-boolean field ('yes') -> false", async () => {
        respond(200, { conversational_debug_enabled: "yes" });
        const { result } = renderHook(() => useConversationalDebugFlag(API, "tok"));
        await waitFor(() => expect(result.current).toBe(false));
    });

    it("missing field -> false", async () => {
        respond(200, {});
        const { result } = renderHook(() => useConversationalDebugFlag(API, "tok"));
        await waitFor(() => expect(result.current).toBe(false));
    });

    it("null token -> no fetch at all, false", async () => {
        let calls = 0;
        global.fetch = jest.fn(() => {
            calls += 1;
            return Promise.resolve(makeResponse(200, { conversational_debug_enabled: true }));
        }) as any;
        const { result } = renderHook(() => useConversationalDebugFlag(API, null));
        await waitFor(() => expect(result.current).toBe(false));
        expect(calls).toBe(0);
    });

    it("null apiUrl -> no fetch at all, false", async () => {
        let calls = 0;
        global.fetch = jest.fn(() => {
            calls += 1;
            return Promise.resolve(makeResponse(200, { conversational_debug_enabled: true }));
        }) as any;
        const { result } = renderHook(() => useConversationalDebugFlag(null, "tok"));
        await waitFor(() => expect(result.current).toBe(false));
        expect(calls).toBe(0);
    });
});
