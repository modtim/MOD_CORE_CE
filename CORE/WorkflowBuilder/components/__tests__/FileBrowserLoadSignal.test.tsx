/**
 * FileBrowser paging signal (GOLDENFIX, 2026-11-03).
 *
 * loadFiles() used to hand fetch the AbortController ITSELF as RequestInit.signal
 * (`signal,` shorthand), which the RequestInit conversion rejects with
 * "Failed to convert value to 'AbortSignal'" — so every listing threw, the catch
 * wiped the list, and a just-created folder row never rendered (wb-ce-golden).
 * The product now passes controller.signal only when the runtime really has an
 * AbortSignal, and a failed reload keeps the previous listing.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { FileBrowser } from '@/components/file-browser';

const FOLDER = 'e2e-gold-dir-1';

function App() {
  return (
    <FileBrowser
      isOpen
      onClose={() => {}}
      accessToken="tok"
      inputType="file"
      allowMultiple={true}
      nodeId="node-1"
    />
  );
}

// Stub fetch: records the RequestInit it was handed, serves one short page.
let seen: any[] = [];
function stubFetch(ok: boolean) {
  seen = [];
  global.fetch = jest.fn((url: string, opts: any) => {
    seen.push(opts);
    if (!ok) throw new TypeError("Failed to read the 'signal' property from 'RequestInit'");
    return {
      ok: true,
      json: () => Promise.resolve([{ id: 'd1', name: FOLDER, cog_type: 'asset_folder', parent_id: null }]),
    };
  }) as any;
}

const realAbortSignal = globalThis.AbortSignal;

beforeEach(() => {
  globalThis.AbortSignal = realAbortSignal;
});

afterEach(() => {
  global.fetch = undefined as any;
});

describe('FileBrowser load signal', () => {
  it('hands fetch a real AbortSignal, not the AbortController', async () => {
    stubFetch(true);
    render(<App />);
    expect(seen.length).toBe(1);
    const sig = seen[0].signal;
    expect(sig).toBeTruthy();
    expect(sig).toBeInstanceOf(AbortSignal);
    expect(sig instanceof AbortController).toBe(false);
    const row = await screen.findByRole('button', { name: FOLDER, exact: true });
    expect(row).toBeTruthy();
  });

  it('omits signal when the runtime has no AbortSignal and still renders', async () => {
    globalThis.AbortSignal = undefined;
    stubFetch(true);
    render(<App />);
    expect(seen.length).toBe(1);
    expect('signal' in seen[0]).toBe(false);
    const row = await screen.findByRole('button', { name: FOLDER, exact: true });
    expect(row).toBeTruthy();
  });

  it('a failed reload keeps the previous listing (no silent wipe)', async () => {
    stubFetch(true);
    render(<App />);
    await screen.findByRole('button', { name: FOLDER, exact: true });
    // Workflow completion re-runs loadFiles; make that reload fail.
    stubFetch(false);
    window.dispatchEvent(new Event('workflow-completed'));
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(screen.queryByRole('button', { name: FOLDER, exact: true })).toBeTruthy();
  });
});
