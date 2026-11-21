/**
 * FX.68.p3 [U] — keyboard access to the canvas menus (WCAG 2.1.1 Keyboard).
 *
 * Two halves, deliberately different mechanisms:
 *
 *  1. The PALETTE half is a real DOM test: NodeLibrary is mounted with the
 *     `onKeyboardAddNode` callback the builder wires, and testing-library's
 *     fireEvent.keyDown fires a real Enter/Space on the focused palette row,
 *     asserting the add callback runs (and refuses on a disabled/retired row).
 *
 *  2. The CANVAS half (Shift+F10 / Escape / arrows / Enter-on-node) lives in
 *     handleCanvasKeyDown inside components/workflow-builder.tsx, which has no
 *     mountable fixture (the builder pulls the wizard panels, dialogs, run
 *     polling and the WebGL node renderers). Same reason buildQueueRowStateMachine
 *     .test.ts source-pins its state machine, these tests pin the handler
 *     contract in the shipped source: the exact key set, the exact close/restore
 *     calls, the focus rings, and that node delete is bound exactly once
 *     (ReactFlow's deleteKeyCode) rather than twice.
 */
import * as fs from 'fs';
import * as path from 'path';
import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import NodeLibrary from '@/components/node-library';

const WB_SRC = fs.readFileSync(
  path.resolve(__dirname, '../components/workflow-builder.tsx'),
  'utf-8',
);

const KEYBOARD_COG = {
  id: 5101, name: 'Keyboard Cog', type: 'General', description: 'add me',
  process_type: 'PROCESS', input_schema: { input_type: 'IMAGE' },
  output_schema: { output_type: 'IMAGE' },
  lifecycle_state: 'active', status: 'active',
};
const RETIRED_COG = {
  ...KEYBOARD_COG, id: 5102, name: 'Retired Cog',
  lifecycle_state: 'retired', status: 'retired',
  retire_at: '2026-11-02T00:00:00Z',
};

const withNodes = (nodes: any[]) => {
  const origFetch = (global as any).fetch;
  (global as any).fetch = () => Promise.resolve({
    ok: true, status: 200,
    json: () => Promise.resolve(nodes),
    text: async () => '[]',
  });
  return () => ((global as any).fetch = origFetch);
};

// Groups render collapsed (radix hides CollapsibleContent); force them open so
// the assertions are about the node rows, same as paletteLifecycleBadges.
jest.mock('@/components/ui/collapsible', () => ({
  Collapsible: ({ children }: any) => children,
  CollapsibleTrigger: ({ children }: any) => children,
  CollapsibleContent: ({ children }: any) => children,
}));

describe('FX.68.p3 palette keyboard add', () => {
  const mount = (nodes: any[], added: any[]) => {
    const restore = withNodes(nodes);
    render(
      <NodeLibrary
        accessToken="tok"
        hasLoadedBefore={true}
        onKeyboardAddNode={(node: any) => added.push(node)}
      />,
    );
    return restore;
  };

  it('Enter on a focused palette row calls the add function', async () => {
    const added: any[] = [];
    const restore = mount([KEYBOARD_COG], added);
    try {
      const row = await screen.findByText('Keyboard Cog');
      const button = row.closest('button');
      fireEvent.keyDown(button as any, { key: 'Enter' });
      expect(added.map((n: any) => n.name)).toEqual(['Keyboard Cog']);
    } finally {
      restore();
    }
  });

  it('Space on a focused palette row calls the add function too', async () => {
    const added: any[] = [];
    const restore = mount([KEYBOARD_COG], added);
    try {
      const row = await screen.findByText('Keyboard Cog');
      fireEvent.keyDown(row.closest('button') as any, { key: ' ', code: 'Space' });
      expect(added.length).toBe(1);
    } finally {
      restore();
    }
  });

  it('a revealed-retired row refuses the keyboard add, same as the drag guard', async () => {
    const added: any[] = [];
    const restore = mount([RETIRED_COG, KEYBOARD_COG], added);
    try {
      // The palette mounts in a loading state until the stubbed fetch resolves,
      // so wait for a visible row before the toggle exists (same as paletteLifecycleBadges).
      await screen.findByText('Keyboard Cog');
      await act(async () => {
        screen.getByText('Show retired', { exact: true }).click();
      });
      const row = await screen.findByText('Retired Cog');
      fireEvent.keyDown(row.closest('button') as any, { key: 'Enter' });
      expect(added).toEqual([]);
    } finally {
      restore();
    }
  });
});

describe('FX.68.p3 canvas keyboard contract (source-pinned)', () => {
  it('Shift+F10 / the ContextMenu key open the node context menu', () => {
    expect(WB_SRC).toMatch(
      /\(e\.key === "F10" && e\.shiftKey\) \|\| e\.key === "ContextMenu"/,
    );
    // Opened from the node's on-screen rect, not the mouse cursor.
    expect(WB_SRC).toMatch(/setNodeCtxMenu\(\{ x: rect\.right, y: rect\.bottom, node \}\)/);
  });

  it('Escape closes the menu and returns focus to where it came from', () => {
    const escape = WB_SRC.slice(
      WB_SRC.indexOf('if (e.key === "Escape" && nodeCtxMenu)'),
      WB_SRC.indexOf('if (e.key === "Escape" && nodeCtxMenu)') + 400,
    );
    expect(escape).toMatch(/setNodeCtxMenu\(null\)/);
    expect(escape).toMatch(/nodeCtxMenuFocusFromRef\.current = null/);
    expect(escape).toMatch(/back\.focus\(\)/);
  });

  it('arrow keys walk the menu items with wrap-around', () => {
    expect(WB_SRC).toMatch(
      /nodeCtxMenu && \/\^Arrow\(Down\|Up\)\$\/\.test\(e\.key\)/,
    );
    expect(WB_SRC).toMatch(/items\[\(at \+ step \+ items\.length\) % items\.length\]/);
  });

  it('the menu is announced as a menu and its items show focus rings', () => {
    expect(WB_SRC).toMatch(/role="menu"/);
    expect(WB_SRC).toMatch(
      /hover:bg-gray-100 focus-visible:ring-2 focus-visible:ring-ring/,
    );
  });

  it('Enter on a focused node opens its config panel', () => {
    const enter = WB_SRC.slice(WB_SRC.indexOf('if (e.key === "Enter") {'));
    expect(enter).toMatch(/if \(e\.key === "Enter"\) \{[\s\S]{0,220}?setSelectedNode\(node\)/);
  });

  it('nodes are Tab-reachable and delete is bound exactly once', () => {
    expect(WB_SRC).toMatch(/^\s*nodesFocusable$/m);
    expect(WB_SRC).toMatch(/deleteKeyCode=\{\['Backspace', 'Delete'\]\}/);
    // No second delete handler: the only Delete/Backspace key logic is the
    // ReactFlow prop above, not a hand-rolled branch.
    expect(WB_SRC).not.toMatch(/e\.key === "Delete"/);
  });

  it('the node wrapper focus ring is painted by CSS, not the renderers', () => {
    const css = fs.readFileSync(
      path.resolve(__dirname, '../app/globals.css'), 'utf-8');
    expect(css).toMatch(/\.react-flow__node:focus-visible/);
  });
});
