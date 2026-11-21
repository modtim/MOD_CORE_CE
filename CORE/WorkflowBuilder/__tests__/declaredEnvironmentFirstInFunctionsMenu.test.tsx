/**
 * 
 * (owner 11-17 ~8:20 PM CT, Gate 7 sitting 3, corrected 8:25 PM CT): in the
 * Workflow Builder Functions menu, the "Declared environment" card must be
 * the FIRST entry in the list — before the Node Wizard button. Text is
 * unchanged; only the render order moved.
 *
 * These tests pin: the Declared environment card is the first child of the
 * functions-menu list, it precedes the Node Wizard button in DOM order, and
 * the sibling order below it is otherwise unchanged (Node Wizard is now the
 * first BUTTON).
 */

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { _resetPlatformFeaturesCache } from '@/lib/platform-features';
import NodeLibrary from '@/components/node-library';

const NODE = {
    id: 5101, name: 'Order Cog', type: 'General', description: 'a node',
    process_type: 'PROCESS', input_schema: { input_type: 'IMAGE' },
    output_schema: { output_type: 'IMAGE' },
    lifecycle_state: 'active', status: 'approved',
};

// jsdom has no fetch: hand the palette GET + the /platform/features GET
// exactly what the component asks for.
const withPayloads = (features: Record<string, unknown>) => {
    const origFetch = (global as any).fetch;
    (global as any).fetch = (url: string) =>
        url.includes('/platform/features')
            ? Promise.resolve({
                  ok: true,
                  status: 200,
                  json: () => Promise.resolve(features),
                  text: async () => '',
              })
            : Promise.resolve({
                  ok: true,
                  status: 200,
                  json: () => Promise.resolve([NODE]),
                  text: async () => '[]',
              });
    return () => ((global as any).fetch = origFetch);
};

const COMPLIANCE_BLOCK = {
    declared_environment: [],
    source: 'default',
    edition: 'ce',
    map_version: 0,
    needs: [],
};

const FEATURES_PAYLOAD = {
    entitlements: {
        edition: 'ce',
        capabilities: [],
        limits: {},
        compliance: COMPLIANCE_BLOCK,
    },
};

describe('Declared environment is first in the Workflow Builder functions list', () => {
    it('renders the Declared environment card as the first child of the functions list', async () => {
        const restore = withPayloads(FEATURES_PAYLOAD);
        try {
            const { container } = render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);

            await waitFor(() =>
                expect(screen.getByText(/Declared environment/i)).toBeInTheDocument(),
            );

            const heading = screen.getByText(/Declared environment/i);
            const card = heading.closest('div');
            expect(card).not.toBeNull();

            const parent = card!.parentElement;
            // The card must be the FIRST child of the functions-menu list.
            expect(parent.firstElementChild).toBe(card);
        } finally {
            restore();
        }
    });

    it('the card precedes the Node Wizard button; Node Wizard is the first button', async () => {
        const restore = withPayloads(FEATURES_PAYLOAD);
        try {
            const { container } = render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);

            await waitFor(() =>
                expect(screen.getByText(/Declared environment/i)).toBeInTheDocument(),
            );

            const buttons = Array.from(container.querySelectorAll('button')).filter(
                (b) => /Wizard|Queue|Approvals|Triggers/.test(b.textContent),
            );
            const texts = buttons.map((b) => b.textContent);
            // The Node Wizard entry (wb-nav-wizard-nodes) is now the first BUTTON
            // in the list; the card sits above it.
            expect(texts[0]).toMatch(/Nodes/);
            expect(texts.length).toBeGreaterThan(1);

            const card = screen.getByText(/Declared environment/i).closest('div');
            const cardIdx = Array.from(container.querySelectorAll('*')).indexOf(card!);
            const wizardIdx = Array.from(container.querySelectorAll('*')).indexOf(buttons[0]);
            expect(cardIdx).toBeLessThan(wizardIdx);
        } finally {
            restore();
        }
    });
});
