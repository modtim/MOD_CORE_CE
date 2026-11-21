"use client";

// EXPR 5.7a — "Why this workflow?" rationale disclosure (standalone, NOT mounted).
//
// The design doc (workflow-wizard-self-explanation) describes a structured
// rationale shape: [{decision_point, chosen_option, alternatives_considered[],
// reason}]. The COMMITTED backend collector (`_rationale_entry` in
// routers/support_chat/_shared.py, persisted by services/wizard_rationale.py)
// does not emit that shape yet — it emits a flatter
// `[{step, rationale}, ...]` with no chosen_option / alternatives_considered
// at all. This component accepts BOTH shapes defensively: the full doc shape
// renders a "Chose: ..." line and alternative buttons; the backend's
// `{step, rationale}` shape maps step -> decision_point and
// rationale -> reason, with no "Chose:" line and no alternatives (there is
// nothing to offer instead). Renders under "Why this workflow?" as a
// collapsed-by-default disclosure; each alternative (when present) is a
// small button that opens a follow-up turn pre-populated via onTryDifferent.
//
// Behaviour contract:
//  - defensive validation only: the array shape, string fields and string[]
//   alternatives are checked at render time; invalid entries are dropped,
//   non-string alternatives are dropped, nothing valid → NOTHING renders;
//  - caps: at most 30 entries, every string truncated to 500 chars;
//  - collapsed by default; the toggle button carries aria-expanded;
//  - alternative buttons exist ONLY when onTryDifferent is provided AND the
//    entry has alternatives;
//  - every server string reaches the DOM as a React text node, never HTML.

import { useState } from "react";

const MAX_ENTRIES = 30;
const MAX_STRING = 500;

interface RationaleEntry {
    decision_point: string;
    /** Empty string when the source entry carries no chosen option (the
     *  committed backend's {step, rationale} shape) — render conditionally. */
    chosen_option: string;
    alternatives_considered: string[];
    reason: string;
}

function truncate(value: string): string {
    return value.length > MAX_STRING ? value.slice(0, MAX_STRING) : value;
}

function sanitizeAlternatives(raw: unknown): string[] {
    const list = Array.isArray(raw) ? raw : [];
    return list
        .filter((alt) => typeof alt === "string")
        .map((alt) => truncate(alt as string));
}

/** Full doc shape: {decision_point, chosen_option, reason, alternatives_considered[]}. */
function fromDocShape(candidate: Record<string, unknown>): RationaleEntry | null {
    const decision_point = candidate.decision_point;
    const chosen_option = candidate.chosen_option;
    const reason = candidate.reason;
    if (
        typeof decision_point !== "string" ||
        typeof chosen_option !== "string" ||
        typeof reason !== "string"
    ) {
        return null;
    }
    return {
        decision_point: truncate(decision_point),
        chosen_option: truncate(chosen_option),
        alternatives_considered: sanitizeAlternatives(candidate.alternatives_considered),
        reason: truncate(reason),
    };
}

/** Committed backend shape (`_rationale_entry` in support_chat/_shared.py,
 *  services/wizard_rationale.py): {step, rationale}. No chosen_option, no
 *  alternatives exist on the wire for this shape. */
function fromBackendShape(candidate: Record<string, unknown>): RationaleEntry | null {
    const step = candidate.step;
    const rationale = candidate.rationale;
    if (typeof step !== "string" || typeof rationale !== "string") return null;
    return {
        decision_point: truncate(step),
        chosen_option: "",
        alternatives_considered: [],
        reason: truncate(rationale),
    };
}

function sanitizeRationale(raw: unknown): RationaleEntry[] {
    if (!Array.isArray(raw)) return [];

    const entries: RationaleEntry[] = [];
    for (const item of raw) {
        if (entries.length >= MAX_ENTRIES) break;
        if (typeof item !== "object" || item === null) continue;

        const candidate = item as Record<string, unknown>;
        const entry = "decision_point" in candidate
            ? fromDocShape(candidate)
            : fromBackendShape(candidate);
        if (entry) entries.push(entry);
    }
    return entries;
}

export interface WizardRationaleProps {
    /** Raw wizard payload — validated defensively, never trusted. */
    rationale: unknown;
    /** Opens a follow-up wizard turn pre-populated with the given prompt.
     *  Absent → alternative buttons are not rendered at all. */
    onTryDifferent?: (prompt: string) => void;
}

export default function WizardRationale({
    rationale,
    onTryDifferent,
}: WizardRationaleProps) {
    const [open, setOpen] = useState(false);

    const entries = sanitizeRationale(rationale);
    if (entries.length === 0) return null;

    const canTryDifferent = typeof onTryDifferent === "function";

    return (
        <div data-wizard-rationale="1" className="space-y-2">
            <button
                type="button"
                data-testid="wizard-rationale-toggle"
                aria-expanded={open}
                aria-label="Why this workflow?"
                className="rounded bg-white/5 hover:bg-white/10 active:bg-white/15 px-3 py-1 text-xs font-medium text-slate-100"
                onClick={() => setOpen((value) => !value)}
            >
                Why this workflow?
            </button>
            {open ? (
                <ul
                    data-wizard-rationale-list="1"
                    data-testid="wizard-rationale-list"
                    className="space-y-2"
                >
                    {entries.map((entry, entryIndex) => (
                        <li
                            key={`wizard-rationale-entry-${entryIndex}`}
                            data-testid={`wizard-rationale-entry-${entryIndex}`}
                            className="space-y-1"
                        >
                            <div className="text-xs font-semibold text-slate-100 dark:text-slate-300">
                                {entry.decision_point}
                            </div>
                            {entry.chosen_option ? (
                                <div className="text-xs text-slate-300 dark:text-slate-400">
                                    Chose: {entry.chosen_option}
                                </div>
                            ) : null}
                            <div className="text-xs text-slate-300 dark:text-slate-400">
                                {entry.reason}
                            </div>
                            {canTryDifferent && entry.alternatives_considered.length > 0 ? (
                                <div className="flex flex-wrap gap-1">
                                    {entry.alternatives_considered.map((alternative, altIndex) => (
                                        <button
                                            key={`wizard-rationale-alt-${entryIndex}-${altIndex}`}
                                            type="button"
                                            data-testid={`wizard-rationale-alt-${entryIndex}-${altIndex}`}
                                            aria-label={`Try "${alternative}" instead`}
                                            className="inline-block rounded bg-white/5 hover:bg-white/10 active:bg-white/15 px-2 py-0.5 text-[10px] font-medium text-slate-200"
                                            onClick={() =>
                                                onTryDifferent(
                                                    `For "${entry.decision_point}", use "${alternative}" instead of "${entry.chosen_option}".`
                                                )
                                            }
                                        >
                                            {alternative}
                                        </button>
                                    ))}
                                </div>
                            ) : null}
                        </li>
                    ))}
                </ul>
            ) : null}
        </div>
    );
}
