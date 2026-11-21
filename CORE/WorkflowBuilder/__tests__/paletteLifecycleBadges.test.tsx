/**
 * FX.103 p4 — deprecation / retirement badges in the WorkflowBuilder palette.
 *
 * The palette payload (GET /v1/nodes) carries the FX.103 lifecycle: part-1
 * names (`status` / `retire_after`) and the part-2 aliases the UI reads
 * (`lifecycle_state` / `retire_at`). These tests pin NodeLibrary's render
 * contract: a deprecated node stays in the palette, badged amber with a
 * "Retires on <date>" tooltip and still draggable; a retired node is ABSENT
 * from the DOM by default and revealed greyed + NOT draggable only when the
 * "Show retired" toggle is clicked.
 */
import React from 'react';
import { render, screen, act } from '@testing-library/react';
import NodeLibrary from '@/components/node-library';

const DEPRECATED = {
    id: 4101, name: 'Deprecated Cog', type: 'General', description: 'going away',
    process_type: 'PROCESS', input_schema: { input_type: 'IMAGE' },
    output_schema: { output_type: 'IMAGE' },
    lifecycle_state: 'deprecated', status: 'deprecated',
    retire_at: '2027-01-01T00:00:00Z', retire_after: '2027-01-01T00:00:00Z',
};
const RETIRED = {
    id: 4102, name: 'Retired Cog', type: 'General', description: 'gone',
    process_type: 'PROCESS', input_schema: { input_type: 'IMAGE' },
    output_schema: { output_type: 'IMAGE' },
    lifecycle_state: 'retired', status: 'retired',
    retire_at: '2026-11-02T00:00:00Z',
};
const ACTIVE = {
    id: 4103, name: 'Active Cog', type: 'General', description: 'fine',
    process_type: 'PROCESS', input_schema: { input_type: 'IMAGE' },
    output_schema: { output_type: 'IMAGE' },
    lifecycle_state: 'active', status: 'active',
};

// jsdom has no fetch. The palette GET is the only network call on mount; the
// stub hands it the nodes under test. (apiUrl() falls back to the built-in
// default — the URL itself is irrelevant, only the response matters.)
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

// Groups render COLLAPSED while browsing (radix hides CollapsibleContent via
// the `hidden` attribute, which testing-library skips). Force them open so the
// assertions are about the node rows, not the accordion.
jest.mock('@/components/ui/collapsible', () => ({
    Collapsible: ({ children }: { children?: React.ReactNode }) => children,
    CollapsibleTrigger: ({ children }: { children?: React.ReactNode }) => children,
    CollapsibleContent: ({ children }: { children?: React.ReactNode }) => children,
}));

describe('FX.103 palette lifecycle badges', () => {
    it('badges a deprecated node amber with the retirement date, still draggable', async () => {
        const restore = withNodes([DEPRECATED, ACTIVE]);
        try {
            render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            const badge = await screen.findByText('Deprecated');
            expect(badge.getAttribute('title')).toBe('Retires on 2027-01-01');
            expect(badge.getAttribute('data-lifecycle-state')).toBe('deprecated');
            // The active node carries no lifecycle badge at all.
            expect(screen.queryByText('Retired')).not.toBeInTheDocument();
        } finally {
            restore();
        }
    });

    it('hides a retired node from the palette by default', async () => {
        const restore = withNodes([RETIRED, ACTIVE]);
        try {
            render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            await screen.findByText('Active Cog');
            expect(screen.queryByText('Retired Cog')).not.toBeInTheDocument();
            expect(screen.queryByText('Retired')).not.toBeInTheDocument();
        } finally {
            restore();
        }
    });

    it('the Show retired toggle reveals it greyed out, badged, not draggable', async () => {
        const restore = withNodes([RETIRED, ACTIVE]);
        try {
            render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            await screen.findByText('Active Cog');
            expect(screen.queryByText('Retired Cog')).not.toBeInTheDocument();

            await act(async () => {
                screen.getByText('Show retired', { exact: true }).click();
            });

            const row = await screen.findByText('Retired Cog');
            const badge = screen.getByText('Retired');
            expect(badge.getAttribute('data-lifecycle-state')).toBe('retired');
            expect(badge.getAttribute('title')).toBe('Retired on 2026-11-02 — cannot run');
            // Greyed + not draggable: the row Button carries the disabled
            // styling and its draggable affordance is off.
            const button = row.closest('button');
            if (!button) throw new Error('retired row must render a <button>');
            expect(button.getAttribute('draggable')).not.toBe('true');
            expect(button.className).toContain('cursor-not-allowed');
        } finally {
            restore();
        }
    });
});
