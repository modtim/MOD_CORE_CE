/**
 * ANS-76 (FIX-CE-WB-MARKETPLACE-HIDE-PRICED-ITEMS-ENTIRELY) — the CE
 * Workflow Builder marketplace must not LIST priced items at all; free items
 * keep their Get link.
 *
 * The live CE list is StackMarketplaceTab: GET /catalog?kind=stack is a CE
 * route (2400GX-storeB 30.5.3 — no edition skip), so the tab renders on CE.
 * The filter point is the tab's `filtered` list (stack-marketplace-tab.tsx:197)
 * — a row with price_cents > 0 is dropped when the caller passes a positive
 * `ce` signal. The store dialog passes its already-resolved `ceEdition`
 * (:170, /platform/features, fail-open) to the tab.
 *
 * Pinned here:
 *  - CE: priced rows are absent entirely (no row, no price chip, no Buy link);
 *  - CE: free rows still render and still carry the Get link;
 *  - paid / unresolved edition (ce false, fail-open): priced rows stay.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { StackMarketplaceTab } from '@/components/stack-marketplace-tab';

const jsonOk = (body: unknown) =>
    ({ ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) }) as unknown as Response;

const LIST = [
    { kind: 'stack', id: 1, name: 'Video Render Farm', description: 'GPU render stack', price_cents: 0, price_tier: null, owned: false },
    { kind: 'stack', id: 2, name: 'ML Training Rig', description: 'Priced stack', price_cents: 1999, price_tier: 'pro', owned: false },
    { kind: 'stack', id: 3, name: 'Null Price Stack', description: 'No price field', price_cents: null, price_tier: null, owned: false },
];

function mockFetch() {
    return jest.fn((url: string) => {
        const u = String(url);
        if (u.includes('/catalog?kind=stack')) return Promise.resolve(jsonOk(LIST));
        return Promise.resolve(jsonOk({}));
    });
}

afterEach(() => { jest.resetAllMocks(); });

describe('CE marketplace list — priced items hidden entirely (ANS-76)', () => {
    it('CE: drops the priced row entirely — no name, no price chip, no Buy link', async () => {
        global.fetch = mockFetch() as unknown as typeof fetch;
        render(<StackMarketplaceTab accessToken="tok" ce={true} />);

        // FIX-CE-WB-STORE-ENTRY-POINTS-...-D71 (11-16): with the store deny
        // p1 (85c1e76b9c) the CE tab degrades to the plain line — the whole
        // list (priced AND free) is gone, so every assertion below still
        // holds, now against an empty store surface.
        await screen.findByText(/not available in MOD Community Edition/);

        // The priced row is gone from the DOM: no name, no row element, no
        // price chip, no store deep-link for it.
        expect(screen.queryByText('ML Training Rig')).not.toBeInTheDocument();
        expect(screen.queryByTestId('stack-row-2')).not.toBeInTheDocument();
        expect(screen.queryByText('$19.99')).not.toBeInTheDocument();
        expect(screen.queryByTestId('stack-open-full-2')).not.toBeInTheDocument();
        // No Buy CTA anywhere on the CE tab.
        expect(screen.queryByText(/Buy/)).not.toBeInTheDocument();
    });

    it('CE: the whole tab degrades to the plain line — no listing at all (D71 supersedes ANS-76)', async () => {
        global.fetch = mockFetch() as unknown as typeof fetch;
        render(<StackMarketplaceTab accessToken="tok" ce={true} />);

        // FIX-CE-WB-STORE-ENTRY-POINTS-...-D71 (11-16, Tim: "CE has no store
        // scope" + "store api routes are Deny!!!"): GET /catalog?kind=stack is
        // DENIED in CE now, so there is no CE listing left to filter — the
        // "free rows keep their Get link" posture (ANS-76, 2400GX-storeB 30.5.3)
        // is superseded: the tab shows ONE plain line and fetches nothing.
        await screen.findByText(/not available in MOD Community Edition/);
        expect(screen.getByTestId('stack-marketplace-ce-unavailable')).toBeInTheDocument();

        expect(screen.queryByText('Video Render Farm')).not.toBeInTheDocument();
        expect(screen.queryByText('Null Price Stack')).not.toBeInTheDocument();
        expect(screen.queryByText('Free')).not.toBeInTheDocument();
        expect(screen.queryByTestId('stack-open-full-1')).not.toBeInTheDocument();
        expect(screen.queryByTestId('stack-marketplace-search')).not.toBeInTheDocument();
    });

    it('paid edition (ce false, fail-open) keeps the priced row and its Buy link', async () => {
        global.fetch = mockFetch() as unknown as typeof fetch;
        render(<StackMarketplaceTab accessToken="tok" />);

        await screen.findByText('Video Render Farm');
        expect(screen.getByText('ML Training Rig')).toBeInTheDocument();
        expect(screen.getByText('$19.99')).toBeInTheDocument();
        // The action anchor lives in the expanded row (same as the paid tab).
        fireEvent.click(screen.getByTestId('stack-row-toggle-2'));
        await waitFor(() => expect(screen.getByTestId('stack-open-full-2')).toBeInTheDocument());
        expect(screen.getByTestId('stack-open-full-2').textContent).toContain('Get');
    });
});
