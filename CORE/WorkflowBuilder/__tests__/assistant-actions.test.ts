/**
 * Tests for the shared assistant ACTION runtime reached through its
 * app-facing entry point `lib/shared/assistant-actions.ts`
 * (mod-assistant-agent §6.4 / sz-mod-assistant-agent.2).
 *
 * These pin the behaviour both panels depend on: the SAME action names, the
 * same shape allow-list, and the same same-origin URL validation as the
 * canonical runtime — the extraction must not have weakened the security
 * logic (isValidRoute / isSafeId / isSafeAnchorSelector / known-type set).
 */

import {
    actionLabel,
    executeAction,
    isValidRoute,
    validateAction,
    writePendingActions,
    readAndClearPendingActions,
} from "@/lib/shared/assistant-actions";

describe("assistant-actions URL allow-list (same-origin validation)", () => {
    it("accepts same-origin relative routes", () => {
        expect(isValidRoute("/billing")).toBe(true);
        expect(isValidRoute("/workers/x")).toBe(true);
    });

    it("rejects absolute, protocol-relative and scheme URLs", () => {
        expect(isValidRoute("https://evil.example.com")).toBe(false);
        expect(isValidRoute("//evil.example.com")).toBe(false);
        expect(isValidRoute("javascript:alert(1)")).toBe(false);
        expect(isValidRoute("billing")).toBe(false);
        expect(isValidRoute("")).toBe(false);
    });
});

describe("assistant-actions action vocabulary", () => {
    it("accepts navigate / open-new-tab / open-dialog with valid targets", () => {
        expect(validateAction({ action: "navigate", url: "/billing" })).toEqual(
            { action: "navigate", url: "/billing" },
        );
        expect(validateAction({ action: "open-new-tab", url: "/docs" })).toEqual(
            { action: "open-new-tab", url: "/docs" },
        );
        expect(validateAction({ action: "open-dialog", id: "stacks-tab" })).toEqual(
            { action: "open-dialog", id: "stacks-tab" },
        );
    });

    it("rejects unknown action types and off-allowlist routes", () => {
        expect(validateAction({ action: "delete-account" })).toBeNull();
        expect(validateAction({ action: "navigate", url: "https://evil.example.com" })).toBeNull();
        expect(validateAction({ action: "navigate", url: "javascript:alert(1)" })).toBeNull();
        expect(validateAction({ action: "open-dialog", id: "#not-an-id" })).toBeNull();
    });

    it("drops only the invalid children of a chain", () => {
        const result = validateAction({
            action: "chain",
            actions: [
                { action: "navigate", url: "/billing" },
                { action: "navigate", url: "http://evil.example.com" },
            ],
        });
        expect(result!.actions).toEqual([{ action: "navigate", url: "/billing" }]);
    });
});

describe("assistant-actions labels + pending handoff", () => {
    it("labels match the panels' button copy", () => {
        expect(actionLabel({ action: "navigate", label: "Billing" })).toBe("Take me to Billing");
        expect(actionLabel({ action: "open-dialog", id: "stacks-tab" })).toBe("Open stacks tab");
    });

    it("pending-action cookie handoff round-trips", () => {
        writePendingActions([{ action: "open-dialog", id: "stacks-tab" }]);
        expect(readAndClearPendingActions()).toEqual([{ action: "open-dialog", id: "stacks-tab" }]);
        expect(readAndClearPendingActions()).toEqual([]);
    });
});

describe("assistant-actions executeAction", () => {
    it("silently drops an unknown action type instead of throwing", () => {
        const warnSpy = jest.spyOn(console, "warn").mockImplementation(() => {});
        let threw: Error | null = null;
        try {
            executeAction({ action: "delete-everything" } as any);
        } catch (e) {
            threw = e as Error;
        }
        expect(threw).toBeNull();
        expect(warnSpy).toHaveBeenCalled();
        warnSpy.mockRestore();
    });

    it("navigates via the resolveNavigateUrl callback", () => {
        const resolve = jest.fn().mockReturnValue("/target");
        try {
            executeAction({ action: "navigate", url: "/billing" }, { resolveNavigateUrl: resolve });
        } catch {
            /* jsdom navigation not implemented — irrelevant */
        }
        expect(resolve).toHaveBeenCalledWith({ action: "navigate", url: "/billing" });
    });
});
