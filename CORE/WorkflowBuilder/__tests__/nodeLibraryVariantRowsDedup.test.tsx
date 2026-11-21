/**
 * CE sweep (wb-ce-batch14) — the node-library search filter resolved TWO
 * buttons for one catalogue node name: strict-mode violation on
 * `getByRole('button', { name: /^HDR Converter/ })` inside the sidebar nav
 * (`data-node-definition-id="1154"` + a second variant row).
 *
 * Root cause: the CE catalogue ships the SAME converter twice under two
 * different `node_kind` values. seed_converters.py:118 writes the row with
 * node_kind "hdr_converter" (:123); the metadata scan registers the manifest
 * row under "hdr_convert_process" (CORE/Metadata/system/
 * HdrConverter_IMAGE_PROCESS_Node.py:5). node_scanner.py:1098 (WS2.7 CE
 * exception) makes keep-listed system rows TENANT-scoped (tenant_id = 1), so
 * crud/node_definition_cud.py's name dedup (which only hides LESS-specific
 * twins) keeps both, and the router's WB14-PRODFIX read-time collapse keys
 * name AND node_kind, so it keeps both too. The palette then renders two rows
 * for one name.
 *
 * This test pins the client-side collapse: two marker-carrying
 * (`ce_included === true`, user_id null) tenant-shared rows for one NAME
 * render ONCE, lowest id first — node_kind is deliberately NOT part of the
 * key, because the keep-list is keyed on name
 * (open-core/node-seed-keeplist.yaml:29, services/ce_inclusion.py:102).
 * Rows without the marker are never collapsed, so a user's own node that
 * shares a catalogue name stays visible.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import NodeLibrary from '@/components/node-library';

// The two rows the CE catalogue actually ships for "HDR Converter". Same
// name, same tenant scope, both stamped by the keep-list — different node_kind.
const HDR_SEEDED = {
    id: 1154,
    name: 'HDR Converter',
    type: 'Converter',
    node_kind: 'hdr_converter',
    tenant_id: 1,
    ce_included: true,
    description: 'Convert HDR scene files to GLB for browser viewing.',
    process_type: 'CPU',
    input_schema: { input_type: 'image' },
    output_schema: { output_type: 'image' },
};
const HDR_MANIFEST = {
    ...HDR_SEEDED,
    id: 1207,
    node_kind: 'hdr_convert_process',
    description: 'Tone-maps HDR and EXR images to standard PNG or JPG.',
};

const OTHER_NODE = {
    id: 2001,
    name: 'Blur Cog',
    type: 'General',
    node_kind: 'image_blur',
    tenant_id: 1,
    ce_included: true,
    description: 'blurs',
    process_type: 'PROCESS',
    input_schema: { input_type: 'IMAGE' },
    output_schema: { output_type: 'IMAGE' },
};

// A user's own node that happens to share a catalogue name. The keep-list
// never stamps it (node_authoring strips a user-supplied ce_included), so it
// carries no marker and must stay visible.
const USER_SAME_NAME = {
    id: 3400,
    name: 'HDR Converter',
    type: 'General',
    node_kind: 'my_hdr_thing',
    tenant_id: 1,
    description: "My own node that shares the catalogue name.",
    process_type: 'PROCESS',
    input_schema: { input_type: 'IMAGE' },
    output_schema: { output_type: 'IMAGE' },
};

// jsdom has no fetch; hand the palette GET the rows under test
// (same stub shape as humanApprovalDedup.test.tsx / paletteLifecycleBadges.test.tsx).
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

// Groups render collapsed (radix hides CollapsibleContent); force them open
// so the assertions count node rows, not accordion state.
jest.mock('@/components/ui/collapsible', () => ({
    Collapsible: ({ children }: { children?: React.ReactNode }) => children,
    CollapsibleTrigger: ({ children }: { children?: React.ReactNode }) => children,
    CollapsibleContent: ({ children }: { children?: React.ReactNode }) => children,
}));

describe('wb-ce-batch14 — one palette row per catalogue node name', () => {
    it('the two CE HDR Converter rows render ONCE (the strict-mode violation)', async () => {
        const restore = withNodes([HDR_SEEDED, HDR_MANIFEST, OTHER_NODE]);
        try {
            render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            await screen.findByText('Blur Cog');
            // Old behaviour: 2 rows (the seed row + the metadata row) — the
            // locator the spec dials resolves 2 buttons and Playwright throws.
            expect(screen.queryAllByText('HDR Converter', { exact: true })).toHaveLength(1);
            expect(screen.queryByText('Blur Cog')).toBeInTheDocument();
        } finally {
            restore();
        }
    });

    it('the collapse is order-independent (lowest id wins)', async () => {
        const restore = withNodes([HDR_MANIFEST, HDR_SEEDED, OTHER_NODE]);
        try {
            render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            await screen.findByText('Blur Cog');
            expect(screen.queryAllByText('HDR Converter', { exact: true })).toHaveLength(1);
        } finally {
            restore();
        }
    });

    it('three stamped rows for one name still collapse to one', async () => {
        const third = { ...HDR_MANIFEST, id: 1301, node_kind: 'hdr_converter' };
        const restore = withNodes([HDR_SEEDED, HDR_MANIFEST, third, OTHER_NODE]);
        try {
            render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            await screen.findByText('Blur Cog');
            expect(screen.queryAllByText('HDR Converter', { exact: true })).toHaveLength(1);
        } finally {
            restore();
        }
    });

    it('a user node that shares the catalogue name still renders', async () => {
        const restore = withNodes([HDR_SEEDED, HDR_MANIFEST, USER_SAME_NAME, OTHER_NODE]);
        try {
            render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            await screen.findByText('Blur Cog');
            // One collapsed catalogue row + the user's own un-marked row.
            expect(screen.queryAllByText('HDR Converter', { exact: true })).toHaveLength(2);
        } finally {
            restore();
        }
    });

    it('two un-marked rows of the same name are never collapsed', async () => {
        const unmarked = { ...HDR_MANIFEST, ce_included: null };
        const restore = withNodes([HDR_SEEDED, unmarked, OTHER_NODE]);
        try {
            render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            await screen.findByText('Blur Cog');
            expect(screen.queryAllByText('HDR Converter', { exact: true })).toHaveLength(2);
        } finally {
            restore();
        }
    });

    it('a single stamped row is untouched by the collapse (no regression)', async () => {
        const restore = withNodes([HDR_SEEDED, OTHER_NODE]);
        try {
            render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            await screen.findByText('Blur Cog');
            expect(screen.queryAllByText('HDR Converter', { exact: true })).toHaveLength(1);
        } finally {
            restore();
        }
    });
});
