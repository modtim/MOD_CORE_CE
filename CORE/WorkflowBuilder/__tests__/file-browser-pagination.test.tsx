/**
 * FB-PAGING — the WB file browser must page through GET /assets/.
 *
 * The endpoint (CORE/API/routers/cogs/assets_cogs_files.py:194-207) defaults to
 * limit=50 and returns a BARE list — no total, no next-page marker — so a single
 * fetch silently hides every child past the 50-item cut (wb-ce-batch25 :62 saw a
 * freshly created folder as "not found" because it sat past the cut).
 *
 * Fix under test: loadFiles loops offset 0,50,100,... until a page shorter than
 * PAGE_LIMIT (cap 20 pages), then filters/sorts the FULL concatenated list.
 *
 * Proved against the OLD file (single fetch) via MCW_OVERRIDE: the 120-child
 * case renders only 50 rows and the offset loop never runs -> red.
 */

import React from 'react';
import { render, screen, cleanup } from '@testing-library/react';
import { FileBrowser } from '@/components/file-browser';

const PAGE_LIMIT = 50;

const makeChildren = (n: number) =>
  Array.from({ length: n }, (_, i) => ({
    id: `id-${i}`,
    name: `file-${i}.png`,
    cog_type: 'asset_file',
    parent_id: null,
  }));

// Offset-aware fake /assets/ endpoint: slices the synthetic folder by the
// `offset` query param, exactly like the real bare-list handler.
function pagingFetch(children: any[]) {
  const calls: string[] = [];
  const fn = jest.fn((url: RequestInfo | URL) => {
    const u = String(url);
    calls.push(u);
    const m = u.match(/offset=(\d+)/);
    const off = m ? Number(m[1]) : 0;
    return Promise.resolve({ ok: true, json: () => Promise.resolve(children.slice(off, off + PAGE_LIMIT)) });
  });
  (global as any).fetch = fn;
  return { fn, calls };
}

// Exact-text matcher: only the name <span> holds exactly "file-N.png" (the row
// container also carries the icon glyph and the type column).
const NAME_RE = /^file-\d+\.png$/;

const offsetsOf = (calls: string[]) =>
  calls.map((u) => Number((u.match(/offset=(\d+)/) || [, '0'])[1]));

// Let the pending promise chains run without depending on their exact hop count:
// a macrotask boundary drains every microtask chain already scheduled.
const settle = () => new Promise((r) => setTimeout(r, 0));

describe('FB-PAGING — file browser pages through /assets/', () => {
  beforeEach(() => { window.localStorage.setItem('wfbViewMode', 'list'); });
  afterEach(() => { cleanup(); jest.restoreAllMocks(); window.localStorage.clear(); (global as any).fetch = undefined; });

  it('renders all 120 children of a folder in 3 offset pages', async () => {
    const { calls } = pagingFetch(makeChildren(120));
    render(<FileBrowser isOpen onClose={() => {}} accessToken="tok" />);

    // The 120th row only exists if paging ran past the 50-item cut.
    await screen.findByText('file-119.png');
    expect(screen.queryAllByText(NAME_RE)).toHaveLength(120);
    // 50 + 50 + 20: the third page is short, so the loop stops there.
    expect(offsetsOf(calls)).toEqual([0, 50, 100]);
  });

  it('a folder of exactly 50 children asks for one page plus the short-page probe, never a third', async () => {
    const { calls } = pagingFetch(makeChildren(50));
    render(<FileBrowser isOpen onClose={() => {}} accessToken="tok" />);

    await screen.findByText('file-49.png');
    expect(screen.queryAllByText(NAME_RE)).toHaveLength(50);
    // A bare-list endpoint cannot report "there is no more" without one probe
    // page, so offset=50 returning [] is the terminator — no offset=100.
    expect(offsetsOf(calls)).toEqual([0, 50]);
  });

  it('a throwing fetch leaves the browser empty (error path preserved)', async () => {
    const fn = jest.fn(() => { throw new Error('network down'); });
    (global as any).fetch = fn;
    render(<FileBrowser isOpen onClose={() => {}} accessToken="tok" />);

    expect(screen.queryAllByText(NAME_RE)).toHaveLength(0);
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('no accessToken -> no fetch at all (auth gate preserved)', async () => {
    const fn = jest.fn() as jest.Mock;
    (global as any).fetch = fn;
    render(<FileBrowser isOpen onClose={() => {}} accessToken={null} />);

    expect(fn).toHaveBeenCalledTimes(0);
    expect(screen.queryAllByText(NAME_RE)).toHaveLength(0);
  });

  // --- stale-response race -------------------------------------------------
  // The paging loop awaited each page sequentially with no cancellation, so a
  // page still in flight when the load's context changed (folder switch, modal
  // close, token refresh — all of which re-run the load effect) resumed into the
  // OLD context and overwrote the new listing with old-folder rows.
  it('a page that resolves after the load context changed never lands in the new listing', async () => {
    const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
    const OLD_RE = /^old-file-\d+\.png$/;
    const NEW_RE = /^new-file-\d+\.png$/;
    const kids = (tag: string) =>
      Array.from({ length: 100 }, (_, i) => ({
        id: `${tag}-id-${i}`,
        name: `${tag}-file-${i}.png`,
        cog_type: 'asset_file',
        parent_id: null,
      }));

    // The mock keys the folder by Authorization header, so the "context change"
    // (old token -> new token) swaps which folder the in-flight pages belong to.
    let releaseOldPage2: (() => void) | null = null;
    const fn = jest.fn((url: RequestInfo | URL, opts: any) => {
      const u = String(url);
      const tok = String(opts?.headers?.Authorization ?? '').replace('Bearer ', '');
      const off = Number((u.match(/offset=(\d+)/) || [, '0'])[1]);
      const page = kids(tok).slice(off, off + PAGE_LIMIT);
      // Only the FIRST load's second page is left in flight; everything else
      // resolves immediately so the ordering below is deterministic.
      if (tok === 'old' && off === PAGE_LIMIT) {
        const p = new Promise<any>((resolve) => {
          releaseOldPage2 = () => resolve({ ok: true, json: () => Promise.resolve(page) });
        });
        return p;
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve(page) });
    });
    (global as any).fetch = fn;

    const { rerender, unmount } = render(<FileBrowser isOpen onClose={() => {}} accessToken="old" />);
    // Page 1 of the old load resolved; page 2 is still awaiting.
    await settle();
    expect(fn.mock.calls.length).toBe(2);

    // Context change: the load effect re-runs, which must cancel the old load.
    rerender(<FileBrowser isOpen onClose={() => {}} accessToken="new" />);
    // The new load finishes while the old page 2 is still in flight.
    await settle();
    expect(fn.mock.calls.length).toBe(5);

    if (releaseOldPage2) releaseOldPage2();
    await settle();

    // The old folder's rows must never paint, and the new listing must be intact.
    expect(screen.queryAllByText(OLD_RE)).toHaveLength(0);
    expect(screen.queryAllByText(NEW_RE)).toHaveLength(100);
    // No act/React "setState after teardown" style warning was emitted.
    expect(errorSpy.mock.calls.filter((c) => String(c[0]).includes('Warning:'))).toHaveLength(0);
    unmount();
  });

  // --- MAX_PAGES truncation ------------------------------------------------
  it('hitting the MAX_PAGES cap on a full page warns once with the item count', async () => {
    const warnSpy = jest.spyOn(console, 'warn').mockImplementation(() => {});
    const { calls } = pagingFetch(makeChildren(20 * PAGE_LIMIT));
    render(<FileBrowser isOpen onClose={() => {}} accessToken="tok" />);

    await screen.findByText(`file-${20 * PAGE_LIMIT - 1}.png`);
    expect(screen.queryAllByText(NAME_RE)).toHaveLength(20 * PAGE_LIMIT);
    // Every page is full, so the loop stops at the cap, not at a short page.
    expect(offsetsOf(calls)).toEqual(Array.from({ length: 20 }, (_, i) => i * PAGE_LIMIT));

    const truncations = warnSpy.mock.calls.filter((c) => String(c[0]).includes('truncated'));
    expect(truncations).toHaveLength(1);
    expect(String(truncations[0][0])).toContain(`${20 * PAGE_LIMIT} items`);
  });
});
