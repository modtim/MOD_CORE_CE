/**
 * FIX-CE-WB-FUNCTIONS-MENU-HIDE-MARKETPLACE-ITEM-ON-CE — the Marketplace
 * button in the WorkflowBuilder functions menu must not render on CE.
 *
 * The guard in node-library.tsx:
 *   `resolved && edition && edition !== "ce"`
 * (same pattern as the store dialog:176 fail-closed).
 *
 * In jsdom without a real entitlements server, useEntitlements() returns
 * edition=null / resolved=false on first render — the fail-closed path.
 * This test asserts the button is NOT rendered in that state.
 *
 * The paid branch (resolved + non-ce edition) is covered by the sibling
 * nodeLibraryFunctionsInScroll tests which render against a resolved
 * entitlements server.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import NodeLibrary from '@/components/node-library';

const NODE = {
    id: 4201, name: 'Scroll Cog', type: 'General', description: 'a node',
    process_type: 'PROCESS', input_schema: { input_type: 'IMAGE' },
    output_schema: { output_type: 'IMAGE' },
    lifecycle_state: 'active', status: 'approved',
};

// jsdom has no fetch: hand the palette GET the nodes under test.
const withNodes = (nodes: any[]) => {
    const origFetch = (global as any).fetch;
    (global as any).fetch = () => Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(nodes),
        text: async () => '[]',
    });
    return () => ((global as any).fetch = origFetch);
};

describe('node-library Marketplace button hidden on CE', () => {
    it('CE / unresolved edition: no Marketplace button in the functions menu', async () => {
        // Without a real entitlements server, edition=null → fail-closed.
        const restore = withNodes([NODE]);
        try {
            const { container } = render(
                <NodeLibrary accessToken="tok" hasLoadedBefore={true} />
            );
            await screen.findByText('General');
            const viewport = container.querySelector(
                '[data-radix-scroll-area-viewport]'
            );
            expect(viewport).not.toBeNull();
            const allButtons = Array.from(
                viewport!.querySelectorAll('button')
            );
            const marketPlace = allButtons.find((b) =>
                b.textContent?.includes('Marketplace')
            );
            expect(marketPlace).toBeUndefined();
        } finally {
            restore();
        }
    });

    // CE: the Marketplace button is denied with the dialog () - no source pin
});
