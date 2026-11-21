/**
 * CE sweep 17 (wb-ce-batch24) — the palette showed TWO "Human Approval"
 * buttons.
 *
 * node-library.tsx pins a client-side entry (id 332) so the gate is visible
 * without a marketplace entitlement, and seed_human_approval_node.py seeds a
 * system NodeDefinition row for the same gate. The row's id is a DB BIGSERIAL
 * (1042 here), so the id-only dedup at the processNodes filter never matched
 * and both rendered.
 *
 * This test pins the fix: the filter also matches on the stable key
 * (`node_kind`), so a server row that duplicates a local entry renders ONCE
 * in both editions.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import NodeLibrary from '@/components/node-library';

// The palette entry as pinned client-side (node-library.tsx localNodes).
const LOCAL_TWIN = {
    id: 332,
    name: 'Human Approval',
    nodeType: 'process',
    type: 'Operators',
    node_kind: 'human_approval',
    description: 'Pauses the run until a person approves or rejects. No worker is held while it waits.',
    process_type: 'CPU',
    cost_in_credits: 0,
    input_schema: { input_type: 'ANY', input_variables: 'NONE' },
    output_schema: { output_type: 'ANY', file_mode: 'both', output_variables: 'NONE' },
};

// The seeded DB row: same node_kind / same name, but its id is a BIGSERIAL
// assigned by the seed, NOT the pinned 332. That mismatch is the bug.
const SERVER_ROW = {
    id: 1042,
    name: 'Human Approval',
    type: 'Operators',
    node_kind: 'human_approval',
    description: 'Pauses the run until a person approves or rejects. No worker is held while it waits.',
    process_type: 'CPU',
    cost_in_credits: 0,
    input_schema: { input_type: 'ANY', input_variables: 'NONE' },
    output_schema: { output_type: 'ANY', file_mode: 'both', output_variables: 'NONE' },
};

const OTHER_NODE = {
    id: 2001,
    name: 'Blur Cog',
    type: 'General',
    node_kind: 'image_blur',
    description: 'blurs',
    process_type: 'PROCESS',
    input_schema: { input_type: 'IMAGE' },
    output_schema: { output_type: 'IMAGE' },
};

// A tenant's own copy of the gate (review ISSUE-1): it keeps the system row's
// `node_kind` but has a different `name` and a non-null `tenant_id`. Server
// dedup is by `name`, so this row is listed — tenant copies ARE the palette, so
// it must render. Only the system seed row (tenant_id null) is the local twin.
const TENANT_COPY = {
    id: 4300,
    name: 'Acme Review Gate',
    type: 'Operators',
    node_kind: 'human_approval',
    tenant_id: 7,
    description: 'Tenant-renamed copy of the approval gate.',
    process_type: 'CPU',
    cost_in_credits: 0,
    input_schema: { input_type: 'ANY', input_variables: 'NONE' },
    output_schema: { output_type: 'ANY', file_mode: 'both', output_variables: 'NONE' },
};

// B24-PRODFIX — the CE case. node_scanner.py:1098 (WS2.7 CE exception) seeds
// keep-listed system nodes as TENANT-scoped rows (tenant_id = 1), so the
// system-scope arm misses the twin. The row carries the platform's seed
// marker `ce_included` (node_scanner.py:1165 stamps TRUE on CREATE for
// keep-listed names; node_authoring.py:2587 returns it on the palette).
const CE_SEEDED_TWIN = {
    id: 5100,
    name: 'Human Approval',
    type: 'Operators',
    node_kind: 'human_approval',
    tenant_id: 1,
    ce_included: true,
    description: 'CE-seeded tenant copy of the approval gate.',
    process_type: 'CPU',
    cost_in_credits: 0,
    input_schema: { input_type: 'ANY', input_variables: 'NONE' },
    output_schema: { output_type: 'ANY', file_mode: 'both', output_variables: 'NONE' },
};

// A tenant's renamed copy of the same gate in CE: same node_kind, tenant_id
// 1, but NOT keep-listed under that name, so the serializer returns no
// marker (node_authoring.py:2587 sets None for rows absent from the flags).
const CE_RENAMED_COPY = {
    ...TENANT_COPY,
    tenant_id: 1,
    ce_included: null,
};

// A user's own node that happens to share the local entry's NAME but is a
// different kind. In CE the keep-list derivation (services/ce_inclusion.py:94)
// matches by name, so this row can carry ce_included=true — it must still
// render, because the dedup is kind-scoped, not name-blind.
const SAME_NAME_OTHER_KIND = {
    ...TENANT_COPY,
    id: 6200,
    name: 'Human Approval',
    node_kind: 'review_gate',
    tenant_id: 1,
    ce_included: true,
    description: 'User node that shares the gate name but is not the gate.',
};

// B24-FIX2 — a user's OWN node that shares BOTH the local entry's kind AND its
// name, but carries no marker (the serializer returns no `ce_included` for a
// row the CE keep-list does not stamp, and the server now refuses a
// user-supplied value — services/node_authoring_service.py strips it on CREATE
// unless the caller is the platform service identity). The kind arm's `=== true`
// guard must keep it visible: hiding is marker-scoped, not kind/name-blind.
const USER_SAME_KIND_UNMARKED = {
    ...TENANT_COPY,
    id: 7300,
    name: 'Human Approval',
    tenant_id: 1,
    description: "User's own gate: same kind, same name, no marker.",
};

// jsdom has no fetch; hand the palette GET the rows under test
// (same stub shape as paletteLifecycleBadges.test.tsx).
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

describe('CE sweep 17 — Human Approval renders once', () => {
    it('a seeded server row with a different id still renders once', async () => {
        const restore = withNodes([SERVER_ROW, OTHER_NODE]);
        try {
            render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            await screen.findByText('Human Approval');
            const rows = screen.queryAllByText('Human Approval', { exact: true });
            expect(rows).toHaveLength(1);
            // The unrelated server row is untouched by the dedup.
            expect(screen.queryByText('Blur Cog')).toBeInTheDocument();
        } finally {
            restore();
        }
    });

    it('the pinned id still dedupes (no regression on the old rule)', async () => {
        const sameId = { ...SERVER_ROW, id: 332 };
        const restore = withNodes([sameId, OTHER_NODE]);
        try {
            render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            await screen.findByText('Human Approval');
            expect(screen.queryAllByText('Human Approval', { exact: true })).toHaveLength(1);
        } finally {
            restore();
        }
    });

    it('local-only palette (no server rows) still shows the gate once', async () => {
        const restore = withNodes([]);
        try {
            render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            await screen.findByText('Human Approval');
            expect(screen.queryAllByText('Human Approval', { exact: true })).toHaveLength(1);
        } finally {
            restore();
        }
    });

    it('a tenant-owned copy of the gate with a different name still renders', async () => {
        const restore = withNodes([SERVER_ROW, TENANT_COPY, OTHER_NODE]);
        try {
            render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            await screen.findByText('Acme Review Gate');
            // The tenant's own gate is not the local twin — it renders.
            expect(screen.queryByText('Acme Review Gate')).toBeInTheDocument();
            // The system seed row is still deduped against the pinned entry.
            expect(screen.queryAllByText('Human Approval', { exact: true })).toHaveLength(1);
        } finally {
            restore();
        }
    });

    // B24-PRODFIX (CE launch) — the CE tenant_id=1 seeded twin.
    it('B24-PRODFIX (a) CE tenant_id=1 seeded twin of the same kind renders once', async () => {
            const restore = withNodes([CE_SEEDED_TWIN, OTHER_NODE]);
            try {
                render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
                await screen.findByText('Human Approval');
                expect(screen.queryAllByText('Human Approval', { exact: true })).toHaveLength(1);
                expect(screen.queryByText('Blur Cog')).toBeInTheDocument();
            } finally {
                restore();
            }
        });

        it('B24-PRODFIX (b) renamed copy of the same kind still renders', async () => {
            const restore = withNodes([CE_SEEDED_TWIN, CE_RENAMED_COPY, OTHER_NODE]);
            try {
                render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
                await screen.findByText('Acme Review Gate');
                expect(screen.queryByText('Acme Review Gate')).toBeInTheDocument();
                expect(screen.queryAllByText('Human Approval', { exact: true })).toHaveLength(1);
            } finally {
                restore();
            }
        });

        it('B24-PRODFIX (c) same-name node of a different kind still renders', async () => {
            const restore = withNodes([SAME_NAME_OTHER_KIND, OTHER_NODE]);
            try {
                render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
                // Two exact matches (pinned local entry + the user's own
                // different-kind node) — findByText would throw on ambiguity,
                // so assert the count directly — after the fetch has resolved
                // (pinned via a unique row).
                await screen.findByText('Blur Cog');
                expect(screen.queryAllByText('Human Approval', { exact: true })).toHaveLength(2);
            } finally {
                restore();
            }
        });

        it('B24-PRODFIX (d) the tenant_id-null system twin is still deduped', async () => {
            const restore = withNodes([SERVER_ROW, OTHER_NODE]);
            try {
                render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
                await screen.findByText('Human Approval');
                expect(screen.queryAllByText('Human Approval', { exact: true })).toHaveLength(1);
            } finally {
                restore();
            }
        });

        it('B24-FIX2 user node of the same kind AND name without the marker stays visible', async () => {
            const restore = withNodes([USER_SAME_KIND_UNMARKED, OTHER_NODE]);
            try {
                render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
                // Two exact matches (pinned local entry + the user's own
                // un-marked row) — same kind, same name, no `ce_included`.
                await screen.findByText('Blur Cog');
                expect(screen.queryAllByText('Human Approval', { exact: true })).toHaveLength(2);
            } finally {
                restore();
            }
        });
});
