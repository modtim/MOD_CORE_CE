/**
 * FX.103 p4 — deprecation / retirement badge on the CANVAS node header.
 *
 * An existing workflow that already contains a node whose NodeDefinition was
 * deprecated (or retired) after the workflow was saved must show the same
 * small badge in the node header the palette shows. The canvas NodeData
 * carries `lifecycleState` / `retireAt` (hydrated at drop time by
 * lib/workflow-utils.ts and refreshed by the node-config panel's live-def
 * fetch); ProcessNode renders the shared LifecycleBadge in the header bar.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';

// Stub heavy child modules irrelevant to the header badge (previews / three.js
// / chips), same pattern as processNodeLoraPorts.test.tsx.
jest.mock('@/components/nodes/asset-preview', () => ({ AssetPreview: () => null }));
jest.mock('@/components/model3d-thumbnail', () => ({ Model3DViewerModal: () => null }));
jest.mock('@/components/nodes/node-stack-overlay', () => ({ NodeStackOverlay: () => null }));

// reactflow's Handle throws outside a real graph ("zustand provider" error),
// so stub it — same pattern as processNodeLoraPorts.test.tsx.
jest.mock('reactflow', () => ({
    Handle: () => null,
    Position: { Top: 'top', Bottom: 'bottom', Left: 'left', Right: 'right' },
}));

import { ProcessNode } from '@/components/nodes/process-node';
import { LifecycleBadge, LIFECYCLE_BADGE_STYLES } from '@/components/node-chips';

const baseData: any = {
    label: 'Upscale',
    description: 'upscales an image',
    process_type: 'PROCESS',
    input_schema: { input: { type: 'image' } },
    output_schema: { output: { type: 'image' } },
};

const renderNode = (data: any) =>
    render(
        <ProcessNode data={data} isConnectable id="n1" selected={false} type="process" zIndex={1} dragging={false} xPos={0} yPos={0} />
    );

describe('ProcessNode lifecycle badge', () => {
    it('renders nothing for an active / absent lifecycle (no badge noise)', () => {
        renderNode(baseData);
        renderNode({ ...baseData, lifecycleState: 'active' });
        expect(screen.queryByText('Deprecated')).not.toBeInTheDocument();
        expect(screen.queryByText('Retired')).not.toBeInTheDocument();
    });

    it('badges a deprecated canvas node amber with the retirement date', () => {
        renderNode({ ...baseData, lifecycleState: 'deprecated', retireAt: '2027-01-01T00:00:00Z' });
        const badge = screen.getByText('Deprecated');
        expect(badge.getAttribute('title')).toBe('Retires on 2027-01-01');
        expect(badge.getAttribute('data-lifecycle-state')).toBe('deprecated');
        expect(badge.className).toContain(LIFECYCLE_BADGE_STYLES.deprecated);
    });

    it('badges a retired canvas node grey saying it cannot run', () => {
        renderNode({ ...baseData, lifecycleState: 'retired', retireAt: '2026-11-02T00:00:00Z' });
        const badge = screen.getByText('Retired');
        expect(badge.getAttribute('title')).toBe('Retired on 2026-11-02 — cannot run');
        expect(badge.getAttribute('data-lifecycle-state')).toBe('retired');
        expect(badge.className).toContain(LIFECYCLE_BADGE_STYLES.retired);
    });

    it('shares the palette badge component (one source of truth)', () => {
        render(<LifecycleBadge lifecycleState="deprecated" retireAt="2027-01-01T00:00:00Z" className="mx-1" />);
        expect(screen.getByText('Deprecated').className).toContain('mx-1');
    });
});
