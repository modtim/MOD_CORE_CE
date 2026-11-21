/**
 * BUG-WB-FUNCTIONS-PANEL — the Functions dropdown must live INSIDE the
 * node-library ScrollArea, as the first Collapsible section, so opening
 * it can no longer push the node accordions out of the overflow-hidden
 * sidebar (owner 11-07: nodes beneath were unreachable while the menu was
 * open). These tests pin: one scroll viewport for both regions, the
 * aria-expanded contract on the trigger, Functions BEFORE the node
 * groups, and no flex-shrink-0 wrapper inside the viewport.
 */
import React from 'react';
import { render, screen, act } from '@testing-library/react';
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

const FUNCTIONS_TITLE = 'Workflow functions: new node, queues, schedule';

describe('node-library Functions section inside the scroll viewport', () => {
    it('renders the Functions trigger inside the ScrollArea viewport', async () => {
        const restore = withNodes([NODE]);
        try {
            const { container } = render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            // The node group trigger is the visible proof the palette loaded.
            // Browsing mode renders the node groups COLLAPSED (radix hides
            // CollapsibleContent via the `hidden` attribute, which testing-
            // library skips), so the group TRIGGER text is the visible
            // proof the palette loaded — the rows themselves are not.
            await screen.findByText('General');
            const viewport = container.querySelector('[data-radix-scroll-area-viewport]');
            if (!viewport) throw new Error('no scroll viewport rendered');
            const trigger = viewport.querySelector(`button[title="${FUNCTIONS_TITLE}"]`);
            expect(trigger).not.toBeNull();
            expect(trigger!.textContent).toContain('Functions');
        } finally {
            restore();
        }
    });

    it('keeps the aria-expanded contract on the Functions trigger', async () => {
        const restore = withNodes([NODE]);
        try {
            const { container } = render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            // Browsing mode renders the node groups COLLAPSED (radix hides
            // CollapsibleContent via the `hidden` attribute, which testing-
            // library skips), so the group TRIGGER text is the visible
            // proof the palette loaded — the rows themselves are not.
            await screen.findByText('General');
            const viewport = container.querySelector('[data-radix-scroll-area-viewport]');
            const trigger = viewport!.querySelector(`button[title="${FUNCTIONS_TITLE}"]`);
            expect(trigger!.getAttribute('aria-expanded')).toBe('true');
            expect(trigger!.getAttribute('data-state')).toBe('open');
        } finally {
            restore();
        }
    });

    it('places the Functions section before the node-group accordions', async () => {
        const restore = withNodes([NODE]);
        try {
            const { container } = render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            // Browsing mode renders the node groups COLLAPSED (radix hides
            // CollapsibleContent via the `hidden` attribute, which testing-
            // library skips), so the group TRIGGER text is the visible
            // proof the palette loaded — the rows themselves are not.
            await screen.findByText('General');
            const viewport = container.querySelector('[data-radix-scroll-area-viewport]');
            const buttons = Array.from(viewport!.querySelectorAll('button'));
            const fnIdx = buttons.findIndex((b) => b.getAttribute('title') === FUNCTIONS_TITLE);
            const groupIdx = buttons.findIndex((b) => b.textContent.trim() === 'General');
            expect(fnIdx).toBeGreaterThanOrEqual(0);
            expect(groupIdx).toBeGreaterThanOrEqual(0);
            expect(fnIdx).toBeLessThan(groupIdx);
        } finally {
            restore();
        }
    });

    it('gives the Functions section no flex-shrink-0 inside the viewport', async () => {
        const restore = withNodes([NODE]);
        try {
            const { container } = render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            // Browsing mode renders the node groups COLLAPSED (radix hides
            // CollapsibleContent via the `hidden` attribute, which testing-
            // library skips), so the group TRIGGER text is the visible
            // proof the palette loaded — the rows themselves are not.
            await screen.findByText('General');
            const viewport = container.querySelector('[data-radix-scroll-area-viewport]');
            const trigger = viewport!.querySelector(`button[title="${FUNCTIONS_TITLE}"]`);
            const wrapper = trigger!.closest('div[data-state]');
            expect(wrapper.getAttribute('class') ?? '').not.toContain('flex-shrink-0');
        } finally {
            restore();
        }
    });

    it('collapsing Functions leaves the node groups reachable', async () => {
        const restore = withNodes([NODE]);
        try {
            const { container } = render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            // Browsing mode renders the node groups COLLAPSED (radix hides
            // CollapsibleContent via the `hidden` attribute, which testing-
            // library skips), so the group TRIGGER text is the visible
            // proof the palette loaded — the rows themselves are not.
            await screen.findByText('General');
            const viewport = container.querySelector('[data-radix-scroll-area-viewport]');
            await act(async () => {
                viewport!.querySelector(`button[title="${FUNCTIONS_TITLE}"]`)!.click();
            });
            const trigger = viewport!.querySelector(`button[title="${FUNCTIONS_TITLE}"]`);
            expect(trigger!.getAttribute('aria-expanded')).toBe('false');
            expect(screen.queryByText('General')).toBeInTheDocument();
        } finally {
            restore();
        }
    });

    it('assertCollapsibleContentHasHeightBound — the Functions CollapsibleContent carries a max-h class', async () => {
        const restore = withNodes([NODE]);
        try {
            const { container } = render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            await screen.findByText('General');
            const viewport = container.querySelector('[data-radix-scroll-area-viewport]');
            // Radix Collapsible renders TWO divs with data-state="open":
            // [0] = wrapper, [1] = CollapsibleContent. Target the second.
            const allOpen = viewport!.querySelectorAll('div[data-state="open"]');
            expect(allOpen.length).toBeGreaterThanOrEqual(2);
            const collapsibleContent = allOpen[1];
            expect(collapsibleContent).not.toBeNull();
            const cls = collapsibleContent!.getAttribute('class') ?? '';
            // The class must contain max-h-[...] (the exact px value is implementation-specific)
            const hasMaxH = /max-h-\[[\d.]+px\]/.test(cls);
            expect(hasMaxH).toBe(true);
        } finally {
            restore();
        }
    });

    it('assertFunctionsContentScrollsInternally — overflow-y-auto on the CollapsibleContent', async () => {
        const restore = withNodes([NODE]);
        try {
            const { container } = render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            await screen.findByText('General');
            const viewport = container.querySelector('[data-radix-scroll-area-viewport]');
            const allOpen = viewport!.querySelectorAll('div[data-state="open"]');
            expect(allOpen.length).toBeGreaterThanOrEqual(2);
            const collapsibleContent = allOpen[1];
            expect(collapsibleContent).not.toBeNull();
            const cls = collapsibleContent!.getAttribute('class') ?? '';
            // The class must contain overflow-y-auto so content exceeding max-h scrolls internally
            expect(cls).toContain('overflow-y-auto');
        } finally {
            restore();
        }
    });

    it('assertScrollAreaFlexGrowPresent — the ScrollArea grows to fill the nav, not h-full', async () => {
        const restore = withNodes([NODE]);
        try {
            const { container } = render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            await screen.findByText('General');
            const viewport = container.querySelector('[data-radix-scroll-area-viewport]');
            expect(viewport).not.toBeNull();
            // The radix ScrollArea Root is the viewport's parent; it carries
            // `relative overflow-hidden` plus the caller's className. The nav
            // (workflow-builder.tsx:1965) is flex-col + overflow-hidden, so the
            // Root must get its height from flex-grow — h-full on the Root can
            // blow past the parent and clip the last rows (comment at
            // workflow-builder.tsx:1955).
            const root = viewport!.parentElement;
            const cls = root!.getAttribute('class') ?? '';
            expect(cls).toContain('overflow-hidden');
            expect(cls).toContain('flex-grow');
            expect(cls).not.toContain('h-full');
        } finally {
            restore();
        }
    });

    it('assertNodeAccordionsInRemainingViewport — the Functions max-h leaves room for the accordions', async () => {
        const restore = withNodes([NODE]);
        try {
            const { container } = render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
            await screen.findByText('General');
            const viewport = container.querySelector('[data-radix-scroll-area-viewport]');
            const allOpen = viewport!.querySelectorAll('div[data-state="open"]');
            expect(allOpen.length).toBeGreaterThanOrEqual(2);
            const collapsibleContent = allOpen[1];
            expect(collapsibleContent).not.toBeNull();
            const cls = collapsibleContent!.getAttribute('class') ?? '';
            // jsdom has no layout, so this is a bound-value proxy for the
            // browser measure: the sidebar viewport is ~720px (Tim's report),
            // so the Functions section must stay under 320px for the node
            // accordions to keep a usable scroll region beneath it. A bound
            // like max-h-[420px] pushes the accordions out of view.
            const m = /max-h-\[(\d+(?:\.\d+)?)px\]/.exec(cls);
            expect(m).not.toBeNull();
            const maxH = m && m[1] ? parseFloat(m[1]) : NaN;
            expect(maxH).toBeLessThanOrEqual(320);
        } finally {
            restore();
        }
    });
});
