/**
 * FIX-CE-WB-MARKETPLACE-CEEDITION-FAIL-CLOSED-UNRESOLVED-STATE (11-14, Tim)
 *
 * Owner decision: FAIL CLOSED for the marketplace `ceEdition` signal. While
 * the edition is UNRESOLVED (still loading, /platform/features errored/rejected,
 * or the legacy `{ edition, error: "unresolved" }` shape, which lib/entitlements
 * turns into a null block -> edition null + resolved false) the WorkflowBuilder
 * marketplace must behave as CE: the paid-only controls stay hidden until the
 * edition is KNOWN. Only a resolved non-CE edition shows them.
 *
 * The store dialog:171 now derives
 *     ceEdition = !(resolved && edition && edition !== "ce")
 * so unresolved/unknown => ceEdition true (CE render: no Curate tab, no
 * "Publish your own nodes" copy, no paid /nodes/marketplace feed) and the
 * Stacks tab receives ce={true}. Resolved editions keep their old value
 * exactly ("ce" -> true, "saas"/"pro" -> false).
 *
 * Pinned here (the resolvable surface — see the note at the bottom):
 *  - StackMarketplaceTab with the fail-closed signal the dialog now passes
 *    (ce={true}): the priced row is absent entirely, no price chip, no Buy CTA;
 *  - StackMarketplaceTab with an explicit resolved-paid signal (ce={false}):
 *    the priced row and its Buy link stay (no behaviour change when resolved);
 *  - the derivation table itself (pure logic, no DOM): unresolved/loading/error
 *    => CE, resolved paid => paid.
 *
 * NOTE: the store dialog DOM cases (Curate tab hidden while unresolved)
 * cannot run here — `@/js/product-names` (the store dialog:25) does not
 * resolve inside the WorkflowBuilder app, so every suite that renders the
 * dialog fails to load. That is pre-existing and documented
 * (REV-OZ-E1-WB-TOTO-...md:41: 12 suites die on
 * "Cannot find module '../js/product-names'"; marketplaceCurate /
 * marketplaceBrowseGate / marketplaceStoreBrowse are among them).
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { StackMarketplaceTab } from '@/components/stack-marketplace-tab';

const jsonOk = (body: unknown) =>
    ({ ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) }) as unknown as Response;

const STACKS = [
    { kind: 'stack', id: 1, name: 'Video Render Farm', description: 'GPU render stack', price_cents: 0, price_tier: null, owned: false },
    { kind: 'stack', id: 2, name: 'ML Training Rig', description: 'Priced stack', price_cents: 1999, price_tier: 'pro', owned: false },
];

function tabFetch() {
    const fetchMock = jest.fn((url: string) => {
        const u = String(url);
        if (u.includes('/catalog?kind=stack')) return Promise.resolve(jsonOk(STACKS));
        return Promise.resolve(jsonOk({}));
    });
    global.fetch = fetchMock as unknown as typeof fetch;
}

afterEach(() => { jest.resetAllMocks(); });

// The dialog's derivation, mirrored for the pure-logic leg (the store dialog
// itself cannot be imported — see the NOTE above). Keep in sync with :171.
const ceEditionOf = (resolved: boolean, edition: string | null) =>
    !(resolved && edition && edition !== 'ce');

describe('Marketplace edition derivation — FAIL CLOSED while unresolved (11-14 Tim)', () => {
    it('unresolved (block null: loading / errored / error shape) => CE', () => {
        expect(ceEditionOf(false, null)).toBe(true);
    });

    it('resolved with no edition field => CE (unknown is not a paid signal)', () => {
        expect(ceEditionOf(true, null)).toBe(true);
    });

    it('resolved "ce" => CE (unchanged)', () => {
        expect(ceEditionOf(true, 'ce')).toBe(true);
    });

    it('resolved non-CE ("saas"/"pro") => paid (unchanged)', () => {
        expect(ceEditionOf(true, 'saas')).toBe(false);
        expect(ceEditionOf(true, 'pro')).toBe(false);
    });
});

describe('StackMarketplaceTab — fail-closed edition signal', () => {
    it('ce={true} (what the dialog passes while the edition is unresolved): priced row absent', async () => {
        tabFetch();
        render(<StackMarketplaceTab accessToken="tok" ce={true} />);

        // FIX-CE-WB-STORE-ENTRY-POINTS-...-D71 (11-16): the store deny p1
        // (85c1e76b9c) makes the whole CE tab degrade to the plain line, so
        // every assertion below still holds against an empty store surface.
        await screen.findByText(/not available in MOD Community Edition/);
        expect(screen.queryByText('ML Training Rig')).not.toBeInTheDocument();
        expect(screen.queryByTestId('stack-row-2')).not.toBeInTheDocument();
        expect(screen.queryByText('$19.99')).not.toBeInTheDocument();
        expect(screen.queryByText(/Buy/)).not.toBeInTheDocument();
    });

    it('ce={false} (resolved paid): priced row and Buy link stay', async () => {
        tabFetch();
        render(<StackMarketplaceTab accessToken="tok" ce={false} />);

        await screen.findByText('Video Render Farm');
        expect(screen.getByText('ML Training Rig')).toBeInTheDocument();
        expect(screen.getByText('$19.99')).toBeInTheDocument();
        fireEvent.click(screen.getByTestId('stack-row-toggle-2'));
        await waitFor(() => expect(screen.getByTestId('stack-open-full-2')).toBeInTheDocument());
        expect(screen.getByTestId('stack-open-full-2').textContent).toContain('Get');
    });
});
