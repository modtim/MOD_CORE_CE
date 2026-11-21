/**
 * workflow-hotkeys — keyboard shortcut dispatch for the WorkflowBuilder canvas.
 *
 * Node deletion is handled natively by ReactFlow's `deleteKeyCode` prop, but
 * Save / Undo / Redo have no native binding, so they are dispatched here.
 *
 * `dispatchWorkflowHotkey` is a pure function so the binding logic (which combo
 * maps to which action, the typing guard, and `preventDefault` semantics) can be
 * unit-tested without mounting the builder. A handler is only invoked — and the
 * event is only `preventDefault`ed — when the corresponding callback is supplied,
 * so an unwired action (e.g. undo with no history stack) is a genuine no-op and
 * the browser's own behavior is left untouched.
 */

export type WorkflowHotkeyAction = "save" | "undo" | "redo" | "copy" | "paste" | "duplicate";

export interface WorkflowHotkeyHandlers {
    onSave?: () => void;
    onUndo?: () => void;
    onRedo?: () => void;
    onCopy?: () => void;
    onPaste?: () => void;
    onDuplicate?: () => void;
}

/**
 * True when the event target is a text-entry surface (input, textarea, select,
 * or contentEditable). Shortcuts must not fire while the user is typing.
 */
export function isTypingTarget(target: EventTarget | null): boolean {
    const el = target as HTMLElement | null;
    if (!el || typeof el.tagName !== "string") return false;
    const tag = el.tagName.toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return true;
    if (el.isContentEditable) return true;
    return false;
}

/**
 * Map a keydown event to a workflow action and invoke the matching handler.
 *
 * Bindings (Ctrl on Windows/Linux, Cmd on macOS):
 *   - Ctrl/Cmd+S              -> save   (preventDefault stops the browser Save dialog)
 *   - Ctrl/Cmd+Z             -> undo
 *   - Ctrl/Cmd+Y             -> redo
 *   - Ctrl/Cmd+Shift+Z       -> redo
 *   - Ctrl/Cmd+C             -> copy      (selected canvas nodes)
 *   - Ctrl/Cmd+V             -> paste     (offset copy of the clipboard)
 *   - Ctrl/Cmd+D             -> duplicate (preventDefault stops browser bookmark)
 *
 * Copy/paste/duplicate only fire when a handler is wired AND (for copy/dup) the
 * handler itself decides there is a selection; when the guard returns null the
 * browser's native clipboard is left alone, so copying text outside the canvas
 * still works.
 *
 * Returns the action taken, or null if the event was ignored (typing, no
 * modifier, unmapped key, or no handler wired for the matched action).
 */
export function dispatchWorkflowHotkey(
    e: KeyboardEvent,
    handlers: WorkflowHotkeyHandlers,
): WorkflowHotkeyAction | null {
    if (isTypingTarget(e.target)) return null;

    const mod = e.ctrlKey || e.metaKey;
    if (!mod || e.altKey) return null;

    const key = e.key.toLowerCase();

    // Save: Ctrl/Cmd+S (no shift, so Ctrl+Shift+S stays free for other uses).
    if (key === "s" && !e.shiftKey) {
        if (handlers.onSave) {
            e.preventDefault();
            handlers.onSave();
            return "save";
        }
        return null;
    }

    // Redo: Ctrl/Cmd+Y, or Ctrl/Cmd+Shift+Z (both are common conventions).
    if (key === "y" || (key === "z" && e.shiftKey)) {
        if (handlers.onRedo) {
            e.preventDefault();
            handlers.onRedo();
            return "redo";
        }
        return null;
    }

    // Undo: Ctrl/Cmd+Z (no shift).
    if (key === "z" && !e.shiftKey) {
        if (handlers.onUndo) {
            e.preventDefault();
            handlers.onUndo();
            return "undo";
        }
        return null;
    }

    // Copy: Ctrl/Cmd+C. No preventDefault — if there is no canvas selection the
    // handler is a no-op and the browser's native copy (e.g. selected text) must
    // still work. The handler owns the "is anything selected?" decision.
    if (key === "c" && !e.shiftKey) {
        if (handlers.onCopy) {
            handlers.onCopy();
            return "copy";
        }
        return null;
    }

    // Paste: Ctrl/Cmd+V. No preventDefault, for the same reason as copy.
    if (key === "v" && !e.shiftKey) {
        if (handlers.onPaste) {
            handlers.onPaste();
            return "paste";
        }
        return null;
    }

    // Duplicate: Ctrl/Cmd+D. preventDefault stops the browser's bookmark dialog.
    if (key === "d" && !e.shiftKey) {
        if (handlers.onDuplicate) {
            e.preventDefault();
            handlers.onDuplicate();
            return "duplicate";
        }
        return null;
    }

    return null;
}
