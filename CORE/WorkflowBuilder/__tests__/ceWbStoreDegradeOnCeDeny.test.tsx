/**
 *  (11-16, Tim)
 *
 * Owner decision 11-16: "CE has no store scope" and "store api routes are
 * Deny!!!" — store deny p1 (85c1e76b9c) puts open-core/manifest.yaml:1901-1905
 * (CORE/API/routers/catalog.py, services/catalog_service.py, crud/
 * node_marketplace_paid.py, services/publisher_verification.py, services/
 * stack_packages.py) on the CE deny list, so GET /catalog?kind=stack and
 * GET /catalog/stack/{id} 404 in CE.
 *
 * The WorkflowBuilder Stacks tab (StackMarketplaceTab) used to fetch both
 * unconditionally (the 2400GX-storeB 30.5.3 premise "GET /catalog?kind=stack
 * is a CE route" is now STALE) and threw on !res.ok, so CE showed a hard
 * "Failed to load stacks: catalog fetch failed (404)" error. The fix: under
 * the caller's fail-closed CE signal (`ce`, the store dialog:176
 * `!(resolved && edition && edition !== "ce")`) the tab renders ONE plain
 * "not available in MOD Community Edition" line and NEVER fetches /catalog;
 * a resolved non-CE (paid) edition keeps the whole previous surface and its
 * fetch path byte-identical.
 *
 * Pinned here:
 *  - CE (ce={true}): ZERO fetches at all, the plain line renders, and no
 *    search box / row / price chip / Buy|Get CTA / detail panel exists;
 *  - paid (ce={false} and the default no-signal render): the fetch happens,
 *    rows + price chips + Buy/Get CTAs render, and the plain line is absent;
 *  - the CE line is the ONLY thing the CE tab shows (supersedes the ANS-76
 *    "CE lists FREE listings only" posture: with /catalog denied there is
 *    no CE listing left to filter).
 *
 * MUTATION GUARD: deleting the `if (ce) { setLoading(false); return; }`
 * guard in stack-marketplace-tab.tsx makes the CE cases red (fetch count >
 * 0 and no plain line); deleting the `{!ce && ...}` render gate makes them
 * red too (rows/CTAs leak onto CE).
 */

import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StackMarketplaceTab } from "@/components/stack-marketplace-tab";

const LIST = [
    { kind: "stack", id: 1, name: "Video Render Farm", description: "GPU render stack", price_cents: 0, price_tier: null, owned: false },
    { kind: "stack", id: 2, name: "ML Training Rig", description: "Priced stack", price_cents: 1999, price_tier: "pro", owned: false },
];

const DETAIL_1 = { ...LIST[0], gpu_requirements: { count: 2, accelerator: "CUDA", vram_gb: 48 }, license_class: "commercial", licence: "commercial", actions: ["get"] };

let fetchCount = 0;
function countingFetch() {
    return jest.fn((url: string) => {
        fetchCount += 1;
        const u = String(url);
        if (u.includes("/catalog/stack/1")) return Promise.resolve({ ok: true, status: 200, json: async () => DETAIL_1, text: async () => "" } as unknown as Response);
        if (u.includes("/catalog?kind=stack")) return Promise.resolve({ ok: true, status: 200, json: async () => LIST, text: async () => "" } as unknown as Response);
        return Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => "" } as unknown as Response);
    });
}

afterEach(() => {
    jest.resetAllMocks();
    fetchCount = 0;
});

describe("WB Stacks tab — CE store entry points degrade on the denied store routes (D71)", () => {
    it("CE (ce={true}, the dialog's fail-closed signal): ZERO fetches, plain line only", async () => {
        global.fetch = countingFetch() as unknown as typeof fetch;
        render(<StackMarketplaceTab accessToken="tok" ce={true} />);

        await screen.findByText(/not available in MOD Community Edition/);
        expect(screen.getByTestId("stack-marketplace-ce-unavailable")).toBeInTheDocument();

        // No /catalog request at all — the denied route is never called.
        expect(fetchCount).toBe(0);

        // Nothing else from the store surface leaks onto CE.
        expect(screen.queryByText("Video Render Farm")).not.toBeInTheDocument();
        expect(screen.queryByText("ML Training Rig")).not.toBeInTheDocument();
        expect(screen.queryByTestId("stack-marketplace-search")).not.toBeInTheDocument();
        expect(screen.queryByTestId("stack-marketplace-loading")).not.toBeInTheDocument();
        expect(screen.queryByTestId("stack-marketplace-empty")).not.toBeInTheDocument();
        expect(screen.queryByTestId("stack-marketplace-error")).not.toBeInTheDocument();
        expect(screen.queryByText(/Buy|Get/)).not.toBeInTheDocument();
        expect(screen.queryByText("$19.99")).not.toBeInTheDocument();
    });

    it("CE: expanding is inert — no detail fetch, no GPU/licence panel", async () => {
        global.fetch = countingFetch() as unknown as typeof fetch;
        render(<StackMarketplaceTab accessToken="tok" ce={true} />);

        await screen.findByText(/not available in MOD Community Edition/);
        expect(fetchCount).toBe(0);
        // There is no row to expand at all.
        expect(screen.queryByTestId("stack-row-toggle-1")).not.toBeInTheDocument();
        expect(screen.queryByTestId("stack-gpu-1")).not.toBeInTheDocument();
        expect(screen.queryByTestId("stack-licence-1")).not.toBeInTheDocument();
    });

    it("paid (ce={false}, resolved non-CE): fetch path and full surface unchanged", async () => {
        global.fetch = countingFetch() as unknown as typeof fetch;
        render(<StackMarketplaceTab accessToken="tok" ce={false} />);

        await screen.findByText("Video Render Farm");
        expect(screen.getByText("ML Training Rig")).toBeInTheDocument();
        expect(screen.getByText("$19.99")).toBeInTheDocument();
        expect(screen.getByTestId("stack-marketplace-search")).toBeInTheDocument();
        // The list fetch happened.
        expect(fetchCount).toBe(1);

        fireEvent.click(screen.getByTestId("stack-row-toggle-1"));
        await waitFor(() => expect(screen.getByTestId("stack-gpu-1")).toBeInTheDocument());
        expect(screen.getByTestId("stack-gpu-1").textContent).toBe("2× CUDA 48 GB");
        // The detail fetch happened too (paid keeps both legs).
        expect(fetchCount).toBe(2);
        expect(screen.queryByTestId("stack-marketplace-ce-unavailable")).not.toBeInTheDocument();
    });

    it("default render (no edition signal at all) keeps the paid surface (tab default ce=false)", async () => {
        global.fetch = countingFetch() as unknown as typeof fetch;
        render(<StackMarketplaceTab accessToken="tok" />);

        await screen.findByText("Video Render Farm");
        expect(screen.getByText("$19.99")).toBeInTheDocument();
        expect(fetchCount).toBe(1);
        expect(screen.queryByTestId("stack-marketplace-ce-unavailable")).not.toBeInTheDocument();
    });
});
