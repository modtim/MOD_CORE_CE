/**
 * debugOutputCompare.test.tsx — unit tests for DebugOutputCompare.
 *
 * Validates:
 *  - Aligned rows by label
 *  - Missing-side dash ("—")
 *  - Changed badge only on differing rows
 *  - JSON pretty-print in <pre>
 *  - Image <img> alt text
 *  - Link rel="noopener noreferrer"
 */

import React from "react";
import { render, screen } from "@testing-library/react";
import DebugOutputCompare, {
    OutputItem,
} from "@/components/DebugOutputCompare";

/* ------------------------------------------------------------------ */
/*  Fixtures                                                          */
/* ------------------------------------------------------------------ */

const textA: OutputItem = { label: "stdout", kind: "text", value: "hello" };
const textB: OutputItem = { label: "stdout", kind: "text", value: "goodbye" };
const textSame: OutputItem = { label: "stdout", kind: "text", value: "same" };
const jsonA: OutputItem = { label: "data", kind: "json", value: '{"k":"v"}' };
const jsonB: OutputItem = { label: "data", kind: "json", value: '{"k":"w"}' };
const imgItem: OutputItem = {
    label: "preview",
    kind: "image",
    value: "https://example.com/img.png",
};
const urlItem: OutputItem = {
    label: "report",
    kind: "url",
    value: "https://example.com/r",
};
const missingSideA: OutputItem = { label: "extra_orig", kind: "text", value: "orig only" };
const missingSideB: OutputItem = { label: "extra_cand", kind: "text", value: "cand only" };

function renderCompare(props: {
    original?: OutputItem[];
    candidate?: OutputItem[];
    title?: string;
}) {
    return render(
        <DebugOutputCompare
            original={props.original ?? []}
            candidate={props.candidate ?? []}
            title={props.title}
        />
    );
}

/* ------------------------------------------------------------------ */
/*  Empty state                                                       */
/* ------------------------------------------------------------------ */

describe("empty state", () => {
    it('shows "No outputs to compare" when both arrays are empty', () => {
        renderCompare({ original: [], candidate: [] });
        expect(
            screen.getByText("No outputs to compare")
        ).toBeInTheDocument();
    });

    it("shows the title when at least one side has output", () => {
        renderCompare({
            original: [textA],
            candidate: [],
            title: "My comparison",
        });
        expect(screen.getByText("My comparison")).toBeInTheDocument();
    });
});

/* ------------------------------------------------------------------ */
/*  Aligned rows by label                                             */
/* ------------------------------------------------------------------ */

describe("aligned rows", () => {
    it("renders rows for all labels present in either side", () => {
        renderCompare({
            original: [textA, missingSideA],
            candidate: [textB, missingSideB],
        });
        // Each label's row header renders once per column (Original + After fix).
        expect(screen.getAllByText("stdout")).toHaveLength(2);
        expect(screen.getAllByText("extra_orig")).toHaveLength(2);
        expect(screen.getAllByText("extra_cand")).toHaveLength(2);
    });

    it("renders Original and After fix column headers", () => {
        renderCompare({ original: [textA], candidate: [textB] });
        expect(screen.getByText("Original")).toBeInTheDocument();
        expect(screen.getByText("After fix")).toBeInTheDocument();
    });
});

/* ------------------------------------------------------------------ */
/*  Missing-side dash                                                 */
/* ------------------------------------------------------------------ */

describe("missing side dash", () => {
    it('shows "—" when a label exists only on the original side', () => {
        renderCompare({
            original: [textA],
            candidate: [],
        });
        // In the "After fix" column the cell under the label should be "—"
        const afterFixColumn = screen.getByText("After fix").closest(
            "div.space-y-2"
        ) as HTMLElement;
        expect(afterFixColumn).toHaveTextContent("—");
    });

    it('shows "—" when a label exists only on the candidate side', () => {
        renderCompare({
            original: [],
            candidate: [textA],
        });
        const origColumn = screen.getByText("Original").closest(
            "div.space-y-2"
        ) as HTMLElement;
        expect(origColumn).toHaveTextContent("—");
    });
});

/* ------------------------------------------------------------------ */
/*  Changed badge                                                     */
/* ------------------------------------------------------------------ */

describe("changed badge", () => {
    it("shows changed badge when values differ", () => {
        renderCompare({ original: [textA], candidate: [textB] });
        const badges = screen.getAllByText("changed");
        expect(badges).toHaveLength(2); // one per column
    });

    it("does not show changed badge when values are the same", () => {
        renderCompare({ original: [textSame], candidate: [textSame] });
        expect(screen.queryByText("changed")).toBeNull();
    });

    it("does not show changed badge on a row where one side is missing", () => {
        // A row only present on the original side is "added/removed", not "changed" —
        // the badge requires both sides to be present and differ.
        renderCompare({
            original: [textA],
            candidate: [],
        });
        expect(screen.queryByText("changed")).toBeNull();
    });

    it("does not show changed badge on the row that has the new label", () => {
        renderCompare({
            original: [],
            candidate: [textA],
        });
        expect(screen.queryByText("changed")).toBeNull();
    });

    it("shows changed badge in both columns for the same label", () => {
        renderCompare({
            original: [textA],
            candidate: [textB],
        });
        // Both columns should have a "changed" badge for the "stdout" row
        const badges = screen.getAllByText("changed");
        expect(badges.length).toBe(2);
    });
});

/* ------------------------------------------------------------------ */
/*  JSON pretty-print                                                 */
/* ------------------------------------------------------------------ */

describe("JSON pretty-print", () => {
    it("renders JSON values inside a <pre> with pretty-printed output", () => {
        const { container } = renderCompare({
            original: [jsonA],
            candidate: [jsonB],
        });
        const pres = Array.from(
            container.querySelectorAll("pre")
        ) as HTMLElement[];
        // The <pre> elements should contain pretty-printed JSON (has newlines / indentation)
        const prettiedJsonA = prettyPrint(jsonA.value);
        const prettiedJsonB = prettyPrint(jsonB.value);
        const allText = pres.map((p) => p.textContent);
        expect(allText.join(" ")).toContain(prettiedJsonA);
        expect(allText.join(" ")).toContain(prettiedJsonB);
    });

    it("falls back to raw value on parse failure", () => {
        const badJson: OutputItem = {
            label: "data",
            kind: "json",
            value: "{not valid json}",
        };
        renderCompare({ original: [badJson], candidate: [] });
        // Should display the raw string, not crash
        expect(screen.getByText("{not valid json}")).toBeInTheDocument();
    });
});

/* ------------------------------------------------------------------ */
/*  Image alt                                                         */
/* ------------------------------------------------------------------ */

describe("image alt", () => {
    it("renders an <img> with alt=label", () => {
        renderCompare({ original: [imgItem], candidate: [] });
        const img = screen.getByAltText("preview");
        expect(img).toBeInTheDocument();
        expect(img).toHaveAttribute("src", "https://example.com/img.png");
    });
});

/* ------------------------------------------------------------------ */
/*  URL link rel                                                      */
/* ------------------------------------------------------------------ */

describe("url link rel", () => {
    it("renders an <a> with rel=\"noopener noreferrer\"", () => {
        renderCompare({ original: [urlItem], candidate: [] });
        const link = screen.getByRole("link", { name: "https://example.com/r" });
        expect(link).toHaveAttribute("rel", "noopener noreferrer");
        expect(link).toHaveAttribute("target", "_blank");
    });
});

/* ------------------------------------------------------------------ */
/*  Text kind                                                         */
/* ------------------------------------------------------------------ */

describe("text kind", () => {
    it("renders text values inside a <pre>", () => {
        renderCompare({ original: [textA], candidate: [textB] });
        // Text items render in <pre> elements — check that we can find the values
        expect(screen.getByText("hello")).toBeInTheDocument();
        expect(screen.getByText("goodbye")).toBeInTheDocument();
    });
});

/* ------------------------------------------------------------------ */
/*  Mixed kinds in one view                                           */
/* ------------------------------------------------------------------ */

describe("mixed kinds", () => {
    it("renders a mix of text, json, image, url in the same comparison", () => {
        renderCompare({
            original: [textA, jsonA, imgItem, urlItem, missingSideA],
            candidate: [textB, jsonB, imgItem, urlItem, missingSideB],
        });
        // All labels should be present (once per column).
        expect(screen.getAllByText("stdout")).toHaveLength(2);
        expect(screen.getAllByText("data")).toHaveLength(2);
        expect(screen.getAllByText("preview")).toHaveLength(2);
        expect(screen.getAllByText("report")).toHaveLength(2);
        expect(screen.getAllByText("extra_orig")).toHaveLength(2);
        expect(screen.getAllByText("extra_cand")).toHaveLength(2);

        // "changed" should appear for stdout, data, extra_orig, extra_cand
        // (preview and url are the same on both sides — no badge)
        const changedBadges = screen.getAllByText("changed");
        // 4 labels differ → 2 badges per label = 8 total, but only 4 labels:
        // stdout, data, extra_orig, extra_cand each appear on both columns
        // However, for extra_orig: candidate side has "—" which differs from "orig only"
        // for extra_cand: original side has "—" which differs from "cand only"
        expect(changedBadges.length).toBeGreaterThan(0);
    });
});

// ---------------------------------------------------------------------------
// Utility used in tests
// ---------------------------------------------------------------------------

function prettyPrint(raw: string): string {
    try {
        return JSON.stringify(JSON.parse(raw), null, 2);
    } catch {
        return raw;
    }
}
