/**
 * wizardRationale.test.tsx — unit tests for WizardRationale (EXPR 5.7a).
 *
 * Validates:
 *  - invalid / empty payload → nothing rendered;
 *  - collapsed by default (toggle carries aria-expanded="false", no list);
 *  - expanding shows every entry (decision_point, "Chose: ...", reason);
 *  - clicking an alternative calls onTryDifferent with the exact prompt;
 *  - no onTryDifferent → no alternative buttons anywhere;
 *  - caps (30 entries) and 500-char truncation;
 *  - non-string alternatives dropped, invalid entries dropped.
 */

import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import WizardRationale from "@/components/WizardRationale";

const ENTRY = {
    decision_point: "Upscale model",
    chosen_option: "Real-ESRGAN",
    alternatives_considered: ["4x-UltraSharp", "Topaz"],
    reason: "Best licence for on-prem tenants.",
};

const LONG = "x".repeat(600);
const TRUNCATED = "x".repeat(500);

function renderRationale(props: { rationale: unknown; onTryDifferent?: (prompt: string) => void }) {
    return render(
        <WizardRationale
            rationale={props.rationale}
            onTryDifferent={props.onTryDifferent}
        />
    );
}

describe("WizardRationale — invalid payload renders nothing", () => {
    it("empty array renders nothing", () => {
        renderRationale({ rationale: [] });
        expect(screen.queryByTestId("wizard-rationale-toggle")).toBeNull();
    });

    it("non-array payload renders nothing", () => {
        renderRationale({ rationale: "why?" });
        expect(screen.queryByTestId("wizard-rationale-toggle")).toBeNull();
    });

    it("entries missing required string fields are dropped", () => {
        renderRationale({
            rationale: [
                { decision_point: "Only point" },
                { decision_point: 1, chosen_option: "a", reason: "b" },
                { decision_point: "a", chosen_option: "b", reason: 5 },
                null,
                "nope",
            ],
        });
        expect(screen.queryByTestId("wizard-rationale-toggle")).toBeNull();
    });

    it("missing alternatives_considered still yields a valid entry", () => {
        renderRationale({
            rationale: [
                { decision_point: "a", chosen_option: "b", reason: "c" },
            ],
        });
        expect(screen.queryAllByTestId("wizard-rationale-toggle")).toHaveLength(1);
        fireEvent.click(screen.getByTestId("wizard-rationale-toggle"));
        expect(screen.queryAllByTestId(/^wizard-rationale-entry-/)).toHaveLength(1);
    });
});

describe("WizardRationale — collapsed by default", () => {
    it("toggle is present, aria-expanded=false, no list rendered", () => {
        renderRationale({ rationale: [ENTRY] });
        const toggle = screen.getByTestId("wizard-rationale-toggle");
        expect(toggle).toHaveAccessibleName("Why this workflow?");
        expect(toggle).toHaveAttribute("aria-expanded", "false");
        expect(screen.queryByTestId("wizard-rationale-list")).toBeNull();
        expect(screen.queryAllByTestId(/^wizard-rationale-entry-/)).toHaveLength(0);
    });

    it("clicking the toggle expands the list", () => {
        renderRationale({ rationale: [ENTRY] });
        const toggle = screen.getByTestId("wizard-rationale-toggle");
        fireEvent.click(toggle);
        expect(toggle).toHaveAttribute("aria-expanded", "true");
        expect(screen.queryAllByTestId("wizard-rationale-list")).toHaveLength(1);
        const rows = screen.queryAllByTestId(/^wizard-rationale-entry-/);
        expect(rows).toHaveLength(1);
        expect(rows[0]).toHaveTextContent("Upscale model");
        expect(rows[0]).toHaveTextContent("Chose: Real-ESRGAN");
        expect(rows[0]).toHaveTextContent("Best licence for on-prem tenants.");
    });

    it("a second click collapses again", () => {
        renderRationale({ rationale: [ENTRY] });
        const toggle = screen.getByTestId("wizard-rationale-toggle");
        fireEvent.click(toggle);
        fireEvent.click(toggle);
        expect(toggle).toHaveAttribute("aria-expanded", "false");
        expect(screen.queryByTestId("wizard-rationale-list")).toBeNull();
    });
});

describe("WizardRationale — try-a-different-choice affordance", () => {
    it("alternative click calls onTryDifferent with the exact prompt", () => {
        const prompts: string[] = [];
        renderRationale({ rationale: [ENTRY], onTryDifferent: (prompt) => prompts.push(prompt) });
        fireEvent.click(screen.getByTestId("wizard-rationale-toggle"));
        fireEvent.click(screen.getByTestId("wizard-rationale-alt-0-1"));
        expect(prompts).toEqual([
            'For "Upscale model", use "Topaz" instead of "Real-ESRGAN".',
        ]);
    });

    it("no onTryDifferent → no alternative buttons rendered", () => {
        renderRationale({ rationale: [ENTRY] });
        fireEvent.click(screen.getByTestId("wizard-rationale-toggle"));
        expect(screen.queryAllByTestId(/^wizard-rationale-alt-/)).toHaveLength(0);
    });

    it("entry with no alternatives renders no buttons", () => {
        renderRationale({
            rationale: [
                { ...ENTRY, alternatives_considered: [] },
            ],
            onTryDifferent: (prompt) => void prompt,
        });
        fireEvent.click(screen.getByTestId("wizard-rationale-toggle"));
        expect(screen.queryAllByTestId(/^wizard-rationale-alt-/)).toHaveLength(0);
    });
});

describe("WizardRationale — committed backend shape ({step, rationale})", () => {
    // The design doc describes {decision_point, chosen_option,
    // alternatives_considered, reason}; the COMMITTED collector
    // (_rationale_entry in CORE/API/routers/support_chat/_shared.py,
    // persisted by services/wizard_rationale.py) actually emits
    // {step, rationale} with no chosen_option / alternatives on the wire.
    const BACKEND_ENTRY = { step: "critic-iter-1", rationale: "Approved: licence-compatible for on-prem." };

    it("renders a real backend-shaped entry: step as heading, rationale as reason, no 'Chose:' line", () => {
        renderRationale({ rationale: [BACKEND_ENTRY] });
        const toggle = screen.getByTestId("wizard-rationale-toggle");
        fireEvent.click(toggle);
        const row = screen.getByTestId("wizard-rationale-entry-0");
        expect(row).toHaveTextContent("critic-iter-1");
        expect(row).toHaveTextContent("Approved: licence-compatible for on-prem.");
        expect(row.textContent).not.toMatch(/Chose:/);
    });

    it("backend-shaped entry has no alternative buttons even with onTryDifferent provided", () => {
        renderRationale({ rationale: [BACKEND_ENTRY], onTryDifferent: (prompt) => void prompt });
        fireEvent.click(screen.getByTestId("wizard-rationale-toggle"));
        expect(screen.queryAllByTestId(/^wizard-rationale-alt-/)).toHaveLength(0);
    });

    it("a mix of doc-shaped and backend-shaped entries both render", () => {
        renderRationale({ rationale: [ENTRY, BACKEND_ENTRY] });
        fireEvent.click(screen.getByTestId("wizard-rationale-toggle"));
        expect(screen.queryAllByTestId(/^wizard-rationale-entry-/)).toHaveLength(2);
        expect(screen.getByTestId("wizard-rationale-entry-0")).toHaveTextContent("Chose: Real-ESRGAN");
        expect(screen.getByTestId("wizard-rationale-entry-1").textContent).not.toMatch(/Chose:/);
    });
});

describe("WizardRationale — caps, truncation, defensive filtering", () => {
    it("35 entries render at most 30", () => {
        const many = Array.from(
            { length: 35 },
            (unused, index) => ({ ...ENTRY, decision_point: `point ${index}` }),
        );
        renderRationale({ rationale: many });
        fireEvent.click(screen.getByTestId("wizard-rationale-toggle"));
        expect(screen.queryAllByTestId(/^wizard-rationale-entry-/)).toHaveLength(30);
    });

    it("long strings are truncated to 500 chars", () => {
        const prompts: string[] = [];
        renderRationale({
            rationale: [
                {
                    decision_point: "d".repeat(600),
                    chosen_option: "c".repeat(600),
                    reason: "r".repeat(600),
                    alternatives_considered: ["a".repeat(600)],
                },
            ],
            onTryDifferent: (prompt) => prompts.push(prompt),
        });
        fireEvent.click(screen.getByTestId("wizard-rationale-toggle"));
        const row = screen.getByTestId("wizard-rationale-entry-0");
        expect(row.textContent).toContain("d".repeat(500));
        expect(row.textContent.includes("d".repeat(501))).toBe(false);
        expect(row.textContent.includes("c".repeat(501))).toBe(false);
        expect(row.textContent.includes("r".repeat(501))).toBe(false);
        fireEvent.click(screen.getByTestId("wizard-rationale-alt-0-0"));
        expect(prompts[0]).toContain("a".repeat(500));
        expect(prompts[0].includes("a".repeat(501))).toBe(false);
    });

    it("non-string alternatives are dropped, strings kept", () => {
        renderRationale({
            rationale: [
                {
                    ...ENTRY,
                    alternatives_considered: ["ok", 7, null, { a: 1 }, ["x"], "also ok"],
                },
            ],
            onTryDifferent: (prompt) => void prompt,
        });
        fireEvent.click(screen.getByTestId("wizard-rationale-toggle"));
        const buttons = screen.queryAllByTestId(/^wizard-rationale-alt-0-/);
        expect(buttons).toHaveLength(2);
        expect(buttons[0]).toHaveAccessibleName('Try "ok" instead');
        expect(buttons[1]).toHaveAccessibleName('Try "also ok" instead');
    });
});
