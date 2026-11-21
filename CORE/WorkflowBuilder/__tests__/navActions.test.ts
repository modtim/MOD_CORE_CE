/**
 * Unit tests for the shared nav-action runtime
 * (mod-assistant-agent §6.4, CORE/WorkflowBuilder/lib/assistant-nav/navActions.ts).
 *
 * Covers: schema validation rejects off-allowlist routes / unknown
 * action types, highlight on a missing element is a no-op, and the
 * BroadcastChannel relay relays "open-state" messages across tabs.
 */
import {
    createBroadcastRelay,
    executeAction,
    highlightElement,
    validateAction,
} from "../lib/assistant-nav/navActions";

// jest's jsdom test environment doesn't expose a global BroadcastChannel
// (jsdom itself has no implementation); polyfill with Node's built-in
// (available since Node 15) so the cross-tab relay test below exercises
// the real message-passing path rather than the SSR/no-op fallback.
if (typeof (globalThis as any).BroadcastChannel === "undefined") {
    (globalThis as any).BroadcastChannel = require("worker_threads").BroadcastChannel;
}

describe("validateAction", () => {
    it("accepts a well-formed navigate action to an allowlisted (relative) route", () => {
        const result = validateAction({ action: "navigate", url: "/billing" });
        expect(result).toEqual({ action: "navigate", url: "/billing" });
    });

    it("rejects a navigate action with an off-allowlist absolute URL", () => {
        expect(validateAction({ action: "navigate", url: "https://evil.example.com" })).toBeNull();
        expect(validateAction({ action: "navigate", url: "javascript:alert(1)" })).toBeNull();
        expect(validateAction({ action: "navigate", url: "//evil.example.com" })).toBeNull();
    });

    it("rejects an unknown action type", () => {
        expect(validateAction({ action: "delete-account" })).toBeNull();
        expect(validateAction({ action: "navigate-and-wipe-db", url: "/billing" })).toBeNull();
    });

    it("rejects non-object input", () => {
        expect(validateAction(null)).toBeNull();
        expect(validateAction(undefined)).toBeNull();
        expect(validateAction("navigate")).toBeNull();
        expect(validateAction(42)).toBeNull();
    });

    it("drops only the invalid children of a chain, keeping valid ones", () => {
        const result = validateAction({
            action: "chain",
            actions: [
                { action: "navigate", url: "/billing" },
                { action: "navigate", url: "http://evil.example.com" },
                { action: "unknown-thing" },
            ],
        });
        expect(result).not.toBeNull();
        expect(result!.actions).toEqual([{ action: "navigate", url: "/billing" }]);
    });

    it("rejects a chain where every child is invalid", () => {
        const result = validateAction({
            action: "chain",
            actions: [{ action: "navigate", url: "http://evil.example.com" }],
        });
        expect(result).toBeNull();
    });

    it("rejects highlight/scroll-to with a non-allowlisted selector", () => {
        expect(validateAction({ action: "highlight", selector: "#anything" })).toBeNull();
        expect(
            validateAction({ action: "highlight", selector: '[data-assistant-anchor="billing-tab"]' }),
        ).not.toBeNull();
    });
});

describe("highlightElement", () => {
    afterEach(() => {
        document.body.innerHTML = "";
    });

    it("is a no-op when the target element is missing", () => {
        expect(() => highlightElement('[data-assistant-anchor="does-not-exist"]')).not.toThrow();
        expect(document.querySelectorAll(".mod-assistant-highlight").length).toBe(0);
    });

    it("is a no-op for an unsafe/unknown selector even if it would match", () => {
        document.body.innerHTML = '<div id="x">hi</div>';
        highlightElement("#x");
        expect(document.getElementById("x")!.classList.contains("mod-assistant-highlight")).toBe(false);
    });

    it("adds the highlight class to an allowlisted anchor that exists", () => {
        document.body.innerHTML = '<div data-assistant-anchor="billing-tab">Billing</div>';
        highlightElement('[data-assistant-anchor="billing-tab"]', 50);
        const el = document.querySelector('[data-assistant-anchor="billing-tab"]')!;
        expect(el.classList.contains("mod-assistant-highlight")).toBe(true);
    });
});

describe("createBroadcastRelay", () => {
    it("relays an open-state message to the onOpenState callback", () => {
        const received: boolean[] = [];
        const relayA = createBroadcastRelay();
        const relayB = createBroadcastRelay((open) => received.push(open));

        relayA.postOpenState(true);

        // jsdom's BroadcastChannel delivers asynchronously via MessageChannel;
        // give it a tick.
        return new Promise((resolve) => setTimeout(resolve, 10)).then(() => {
            expect(received).toEqual([true]);
            relayA.close();
            relayB.close();
        });
    });
});

describe("executeAction", () => {
    it("silently drops an unknown action type instead of throwing", () => {
        const warnSpy = jest.spyOn(console, "warn").mockImplementation(() => {});
        expect(() => executeAction({ action: "delete-everything" } as any)).not.toThrow();
        expect(warnSpy).toHaveBeenCalled();
        warnSpy.mockRestore();
    });

    it("navigates via the resolveNavigateUrl callback for a valid navigate action", () => {
        const resolveNavigateUrl = jest.fn().mockReturnValue("/target");
        // jsdom logs (but doesn't throw on) unimplemented navigation when
        // window.location.href is assigned; we only care that the
        // resolver was invoked with the validated action.
        try {
            executeAction({ action: "navigate", url: "/billing" }, { resolveNavigateUrl });
        } catch {
            /* jsdom navigation not implemented — irrelevant to this assertion */
        }
        expect(resolveNavigateUrl).toHaveBeenCalledWith({ action: "navigate", url: "/billing" });
    });
});
