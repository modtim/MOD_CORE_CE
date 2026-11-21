/**
 * GX.sz-tcp.25 — owner decision 10-10: each run is priced exactly from its
 * input size and the user sees the LOCKED price before Run.
 *
 * RunQuoteDisplay fetches POST /road/{id}/quote (sz-tcp.18) and renders
 * the total credits, the "locked price" note, and the per-node breakdown.
 * It must never block Execute: a fetch error falls back to a non-blocking
 * "standard rate" message instead of hiding the Run path.
 */

import React from "react";
import { render, screen, waitFor } from "@testing-library/react";

jest.mock("@/lib/api-client", () => ({
    authenticatedFetch: jest.fn(),
}));
import { authenticatedFetch } from "@/lib/api-client";
import { RunQuoteDisplay } from "@/components/RunQuoteDisplay";

const mockQuote = (payload: Record<string, unknown>) => {
    (authenticatedFetch as jest.Mock).mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve(payload),
    });
};

beforeEach(() => {
    jest.clearAllMocks();
});

describe("RunQuoteDisplay", () => {
    it("renders the total credits and the locked-price note", async () => {
        mockQuote({
            workflow_id: 1,
            total_credits: 7,
            breakdown: [
                { node_id: "n1", size_tier: "small", credits: 3, basis: "tiered", pricing_source: "exact", predicted_runtime_seconds: 1.2 },
                { node_id: "n2", size_tier: "medium", credits: 4, basis: "tiered", pricing_source: "tier", predicted_runtime_seconds: null },
            ],
            confidence: "estimated",
        });

        render(<RunQuoteDisplay workflowId={1} accessToken="tok" />);

        expect(await screen.findByText("7 credits")).toBeInTheDocument();
        expect(
            screen.getByText("Nothing is charged.")
        ).toBeInTheDocument();
        expect(authenticatedFetch).toHaveBeenCalledWith(
            expect.stringContaining("/road/1/quote"),
            expect.objectContaining({ method: "POST" })
        );
    });

    it("free workflow shows 'Free' instead of a credit count", async () => {
        mockQuote({
            workflow_id: 2,
            total_credits: 0,
            breakdown: [
                { node_id: "n1", size_tier: "small", credits: 0, basis: "flat", pricing_source: "free", predicted_runtime_seconds: null },
            ],
            confidence: "estimated",
        });

        render(<RunQuoteDisplay workflowId={2} accessToken="tok" />);

        expect(await screen.findByText("Free")).toBeInTheDocument();
        expect(screen.queryByText(/0 credits/)).not.toBeInTheDocument();
    });

    it("error path shows a non-blocking fallback message, not an error that hides Run", async () => {
        (authenticatedFetch as jest.Mock).mockResolvedValue({
            ok: false,
            status: 500,
            json: () => Promise.resolve({}),
        });

        render(<RunQuoteDisplay workflowId={3} accessToken="tok" />);

        expect(
            await screen.findByText(
                "Couldn't get a price — you can still run."
            )
        ).toBeInTheDocument();
    });

    it("renders nothing when there is no saved workflow id yet", () => {
        const { container } = render(<RunQuoteDisplay workflowId={null} accessToken="tok" />);
        expect(container).toBeEmptyDOMElement();
    });
});
