/**
 * Tests for the WorkflowBuilder canvas keyboard shortcut dispatcher.
 *
 * Covers the three bindings that were previously dead in the running UI:
 *   - Ctrl/Cmd+S             -> save (with preventDefault so the browser Save
 *                                     dialog doesn't swallow it)
 *   - Ctrl/Cmd+Z             -> undo
 *   - Ctrl/Cmd+Y             -> redo
 *   - Ctrl/Cmd+Shift+Z       -> redo
 * plus the typing guard and the no-op-when-unwired contract.
 */

import {
    dispatchWorkflowHotkey,
    isTypingTarget,
    type WorkflowHotkeyHandlers,
} from "../lib/workflow-hotkeys";

type KeyInit = {
    key: string;
    ctrlKey?: boolean;
    metaKey?: boolean;
    shiftKey?: boolean;
    altKey?: boolean;
    target?: EventTarget | null;
};

function makeEvent(init: KeyInit): KeyboardEvent & { prevented: boolean } {
    const e = {
        key: init.key,
        ctrlKey: init.ctrlKey ?? false,
        metaKey: init.metaKey ?? false,
        shiftKey: init.shiftKey ?? false,
        altKey: init.altKey ?? false,
        target: init.target ?? null,
        prevented: false,
        preventDefault() {
            (this as unknown as { prevented: boolean }).prevented = true;
        },
    };
    return e as unknown as KeyboardEvent & { prevented: boolean };
}

function makeHandlers() {
    const calls: string[] = [];
    const handlers: WorkflowHotkeyHandlers = {
        onSave: () => calls.push("save"),
        onUndo: () => calls.push("undo"),
        onRedo: () => calls.push("redo"),
    };
    return { handlers, calls };
}

describe("dispatchWorkflowHotkey", () => {
    describe("Ctrl+S / Cmd+S -> save", () => {
        it("fires save and prevents the browser default (Ctrl)", () => {
            const { handlers, calls } = makeHandlers();
            const e = makeEvent({ key: "s", ctrlKey: true });
            expect(dispatchWorkflowHotkey(e, handlers)).toBe("save");
            expect(calls).toEqual(["save"]);
            expect(e.prevented).toBe(true);
        });

        it("fires save on Cmd+S (macOS)", () => {
            const { handlers, calls } = makeHandlers();
            const e = makeEvent({ key: "s", metaKey: true });
            expect(dispatchWorkflowHotkey(e, handlers)).toBe("save");
            expect(calls).toEqual(["save"]);
            expect(e.prevented).toBe(true);
        });

        it("is case-insensitive on the key (Shift not required)", () => {
            const { handlers } = makeHandlers();
            const e = makeEvent({ key: "S", ctrlKey: true });
            expect(dispatchWorkflowHotkey(e, handlers)).toBe("save");
        });

        it("ignores a bare 's' with no modifier", () => {
            const { handlers, calls } = makeHandlers();
            const e = makeEvent({ key: "s" });
            expect(dispatchWorkflowHotkey(e, handlers)).toBeNull();
            expect(calls).toEqual([]);
            expect(e.prevented).toBe(false);
        });

        it("does not treat Ctrl+Shift+S as save", () => {
            const { handlers, calls } = makeHandlers();
            const e = makeEvent({ key: "s", ctrlKey: true, shiftKey: true });
            expect(dispatchWorkflowHotkey(e, handlers)).toBeNull();
            expect(calls).toEqual([]);
        });
    });

    describe("Ctrl+Z -> undo", () => {
        it("fires undo and prevents default", () => {
            const { handlers, calls } = makeHandlers();
            const e = makeEvent({ key: "z", ctrlKey: true });
            expect(dispatchWorkflowHotkey(e, handlers)).toBe("undo");
            expect(calls).toEqual(["undo"]);
            expect(e.prevented).toBe(true);
        });

        it("fires undo on Cmd+Z", () => {
            const { handlers, calls } = makeHandlers();
            const e = makeEvent({ key: "z", metaKey: true });
            expect(dispatchWorkflowHotkey(e, handlers)).toBe("undo");
            expect(calls).toEqual(["undo"]);
        });
    });

    describe("Ctrl+Y and Ctrl+Shift+Z -> redo", () => {
        it("fires redo on Ctrl+Y", () => {
            const { handlers, calls } = makeHandlers();
            const e = makeEvent({ key: "y", ctrlKey: true });
            expect(dispatchWorkflowHotkey(e, handlers)).toBe("redo");
            expect(calls).toEqual(["redo"]);
            expect(e.prevented).toBe(true);
        });

        it("fires redo on Ctrl+Shift+Z (not undo)", () => {
            const { handlers, calls } = makeHandlers();
            const e = makeEvent({ key: "z", ctrlKey: true, shiftKey: true });
            expect(dispatchWorkflowHotkey(e, handlers)).toBe("redo");
            expect(calls).toEqual(["redo"]);
            expect(e.prevented).toBe(true);
        });

        it("fires redo on Cmd+Shift+Z (macOS)", () => {
            const { handlers, calls } = makeHandlers();
            const e = makeEvent({ key: "z", metaKey: true, shiftKey: true });
            expect(dispatchWorkflowHotkey(e, handlers)).toBe("redo");
        });
    });

    describe("Ctrl+C / V / D -> copy / paste / duplicate", () => {
        function makeClipboardHandlers() {
            const calls: string[] = [];
            const handlers: WorkflowHotkeyHandlers = {
                onCopy: () => calls.push("copy"),
                onPaste: () => calls.push("paste"),
                onDuplicate: () => calls.push("duplicate"),
            };
            return { handlers, calls };
        }

        it("fires copy on Ctrl+C without preventing default (native copy must survive)", () => {
            const { handlers, calls } = makeClipboardHandlers();
            const e = makeEvent({ key: "c", ctrlKey: true });
            expect(dispatchWorkflowHotkey(e, handlers)).toBe("copy");
            expect(calls).toEqual(["copy"]);
            expect(e.prevented).toBe(false);
        });

        it("fires paste on Cmd+V without preventing default", () => {
            const { handlers, calls } = makeClipboardHandlers();
            const e = makeEvent({ key: "v", metaKey: true });
            expect(dispatchWorkflowHotkey(e, handlers)).toBe("paste");
            expect(calls).toEqual(["paste"]);
            expect(e.prevented).toBe(false);
        });

        it("fires duplicate on Ctrl+D and prevents the browser bookmark dialog", () => {
            const { handlers, calls } = makeClipboardHandlers();
            const e = makeEvent({ key: "d", ctrlKey: true });
            expect(dispatchWorkflowHotkey(e, handlers)).toBe("duplicate");
            expect(calls).toEqual(["duplicate"]);
            expect(e.prevented).toBe(true);
        });

        it("does not hijack copy/paste when no clipboard handlers are wired", () => {
            const handlers: WorkflowHotkeyHandlers = { onSave: () => {} };
            const c = makeEvent({ key: "c", ctrlKey: true });
            const v = makeEvent({ key: "v", ctrlKey: true });
            expect(dispatchWorkflowHotkey(c, handlers)).toBeNull();
            expect(dispatchWorkflowHotkey(v, handlers)).toBeNull();
            expect(c.prevented).toBe(false);
            expect(v.prevented).toBe(false);
        });

        it("does not treat Ctrl+Shift+C as copy", () => {
            const { handlers, calls } = makeClipboardHandlers();
            const e = makeEvent({ key: "c", ctrlKey: true, shiftKey: true });
            expect(dispatchWorkflowHotkey(e, handlers)).toBeNull();
            expect(calls).toEqual([]);
        });
    });

    describe("typing guard", () => {
        it.each(["input", "textarea", "select"])(
            "ignores shortcuts while typing in a <%s>",
            (tag) => {
                const { handlers, calls } = makeHandlers();
                const target = document.createElement(tag);
                const e = makeEvent({ key: "s", ctrlKey: true, target });
                expect(dispatchWorkflowHotkey(e, handlers)).toBeNull();
                expect(calls).toEqual([]);
                expect(e.prevented).toBe(false);
            },
        );

        it("ignores shortcuts in a contentEditable element", () => {
            const { handlers, calls } = makeHandlers();
            const target = document.createElement("div");
            target.setAttribute("contenteditable", "true");
            Object.defineProperty(target, "isContentEditable", { value: true });
            const e = makeEvent({ key: "z", ctrlKey: true, target });
            expect(dispatchWorkflowHotkey(e, handlers)).toBeNull();
            expect(calls).toEqual([]);
        });

        it("still fires on the canvas (non-typing target)", () => {
            const { handlers } = makeHandlers();
            const target = document.createElement("div");
            const e = makeEvent({ key: "s", ctrlKey: true, target });
            expect(dispatchWorkflowHotkey(e, handlers)).toBe("save");
        });
    });

    describe("no-op when a handler is not wired", () => {
        it("does not preventDefault undo/redo when no history handlers exist", () => {
            // Mirrors the builder today: only onSave is supplied, so Ctrl+Z/Y
            // must stay inert rather than swallowing the keystroke.
            const handlers: WorkflowHotkeyHandlers = { onSave: () => {} };
            const undo = makeEvent({ key: "z", ctrlKey: true });
            const redoY = makeEvent({ key: "y", ctrlKey: true });
            const redoShiftZ = makeEvent({ key: "z", ctrlKey: true, shiftKey: true });
            expect(dispatchWorkflowHotkey(undo, handlers)).toBeNull();
            expect(dispatchWorkflowHotkey(redoY, handlers)).toBeNull();
            expect(dispatchWorkflowHotkey(redoShiftZ, handlers)).toBeNull();
            expect(undo.prevented).toBe(false);
            expect(redoY.prevented).toBe(false);
            expect(redoShiftZ.prevented).toBe(false);
        });

        it("ignores unmapped modified keys", () => {
            const { handlers } = makeHandlers();
            expect(dispatchWorkflowHotkey(makeEvent({ key: "a", ctrlKey: true }), handlers)).toBeNull();
        });

        it("ignores Alt-modified combos", () => {
            const { handlers } = makeHandlers();
            expect(
                dispatchWorkflowHotkey(makeEvent({ key: "s", ctrlKey: true, altKey: true }), handlers),
            ).toBeNull();
        });
    });
});

describe("isTypingTarget", () => {
    it("returns false for null", () => {
        expect(isTypingTarget(null)).toBe(false);
    });

    it("returns true for form fields", () => {
        expect(isTypingTarget(document.createElement("input"))).toBe(true);
        expect(isTypingTarget(document.createElement("textarea"))).toBe(true);
        expect(isTypingTarget(document.createElement("select"))).toBe(true);
    });

    it("returns false for a plain div", () => {
        expect(isTypingTarget(document.createElement("div"))).toBe(false);
    });
});
