/**
 * Tests for the WorkflowBuilder file picker (components/file-browser.tsx):
 *   1. Shift-click RANGE selection (contiguous, file-explorer semantics).
 *   2. Ctrl/cmd-click multi-select still toggles individual files.
 *   3. Image previews for formats the browser cannot paint (exr/tif/...) do
 *      NOT fire a doomed /download request — instead they request the
 *      converter-produced /thumbnail (hdr-converter PNG / Pillow WebP) and
 *      render it, falling back to a labeled placeholder only when none exists.
 *
 * Range-select tests run in LIST view (plain filenames; the grid view injects
 * zero-width spaces for wrapping). The preview-format test runs in GRID view
 * because thumbnails (FileThumbnail) only render there.
 *
 * Render-layer tests; the visual result still needs an in-browser confirm — see
 * MANUAL_TEST_FILE_PREVIEW_AND_RANGE_SELECT.md.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { FileBrowser } from '@/components/file-browser';

type Item = { id: string; name: string; cog_type: string; parent_id: string | null };

const makeItems = (): Item[] => [
  { id: '1', name: 'img0.png', cog_type: 'asset_file', parent_id: null },
  { id: '2', name: 'img1.png', cog_type: 'asset_file', parent_id: null },
  { id: '3', name: 'img2.png', cog_type: 'asset_file', parent_id: null },
  { id: '4', name: 'img3.png', cog_type: 'asset_file', parent_id: null },
];

function mockFetch(items: Item[]) {
  const calls: string[] = [];
  const fn = jest.fn((url: RequestInfo | URL) => {
    const u = String(url);
    calls.push(u);
    if (
      u.includes('/download') || u.includes('/thumbnail') ||
      u.includes('/preview-url') || u.includes('/text-content')
    ) {
      return Promise.resolve({ ok: false, json: () => Promise.resolve({}), text: () => Promise.resolve('') });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve(items) });
  });
  (global as any).fetch = fn;
  return { fn, calls };
}

const norm = (s: string) => s.replace(/\u200B/g, '');

// LIST view: filenames render plainly; the clickable row is the ancestor with
// a grid-template-columns style.
function clickRow(name: string, opts: MouseEventInit = {}) {
  const span = screen.getByText(name);
  const row = span.closest('div[style*="grid-template-columns"]');
  fireEvent.click(row as Element, opts);
}

describe('FileBrowser selection (list view)', () => {
  beforeEach(() => { window.localStorage.setItem('wfbViewMode', 'list'); });
  afterEach(() => { jest.restoreAllMocks(); window.localStorage.clear(); });

  it('shift-click selects the inclusive contiguous range', async () => {
    mockFetch(makeItems());
    const onFileSelect = jest.fn();
    render(<FileBrowser isOpen onClose={() => {}} accessToken="tok" onFileSelect={onFileSelect} allowMultiple />);
    await screen.findByText('img0.png', { timeout: 20000 });

    clickRow('img0.png');                    // anchor
    clickRow('img3.png', { shiftKey: true }); // range to last

    fireEvent.click(screen.getByText('Select'));

    expect(onFileSelect).toHaveBeenCalledTimes(1);
    const passed = onFileSelect.mock.calls[0][0] as Item[];
    expect(passed.map(f => f.id).sort()).toEqual(['1', '2', '3', '4']);
  });

  it('shift-click works upward (anchor after the clicked item)', async () => {
    mockFetch(makeItems());
    const onFileSelect = jest.fn();
    render(<FileBrowser isOpen onClose={() => {}} accessToken="tok" onFileSelect={onFileSelect} allowMultiple />);
    await screen.findByText('img0.png', { timeout: 20000 });

    clickRow('img2.png');                     // anchor at index 2
    clickRow('img0.png', { shiftKey: true });  // range upward

    fireEvent.click(screen.getByText('Select'));
    const passed = onFileSelect.mock.calls[0][0] as Item[];
    expect(passed.map(f => f.id).sort()).toEqual(['1', '2', '3']);
  });

  it('ctrl-click toggles individual files (multi-select preserved)', async () => {
    mockFetch(makeItems());
    const onFileSelect = jest.fn();
    render(<FileBrowser isOpen onClose={() => {}} accessToken="tok" onFileSelect={onFileSelect} allowMultiple />);
    await screen.findByText('img0.png', { timeout: 20000 });

    clickRow('img0.png', { ctrlKey: true });
    clickRow('img2.png', { ctrlKey: true });

    fireEvent.click(screen.getByText('Select'));
    const passed = onFileSelect.mock.calls[0][0] as Item[];
    expect(passed.map(f => f.id).sort()).toEqual(['1', '3']);
  });

  it('a plain click after a range can un-toggle a member', async () => {
    mockFetch(makeItems());
    const onFileSelect = jest.fn();
    render(<FileBrowser isOpen onClose={() => {}} accessToken="tok" onFileSelect={onFileSelect} allowMultiple />);
    await screen.findByText('img0.png', { timeout: 20000 });

    clickRow('img0.png');
    clickRow('img2.png', { shiftKey: true }); // selects 1,2,3
    clickRow('img1.png');                      // toggle 2 OFF

    fireEvent.click(screen.getByText('Select'));
    const passed = onFileSelect.mock.calls[0][0] as Item[];
    expect(passed.map(f => f.id).sort()).toEqual(['1', '3']);
  });

  it('single-select mode ignores shift and keeps one file', async () => {
    mockFetch(makeItems());
    const onFileSelect = jest.fn();
    render(<FileBrowser isOpen onClose={() => {}} accessToken="tok" onFileSelect={onFileSelect} allowMultiple={false} />);
    await screen.findByText('img0.png', { timeout: 20000 });

    clickRow('img0.png');
    clickRow('img3.png', { shiftKey: true });

    fireEvent.click(screen.getByText('Select'));
    const passed = onFileSelect.mock.calls[0][0] as Item[];
    expect(passed.map(f => f.id)).toEqual(['4']);
  });
});

describe('FileBrowser image previews (grid view)', () => {
  beforeEach(() => { window.localStorage.setItem('wfbViewMode', 'grid'); });
  afterEach(() => { jest.restoreAllMocks(); window.localStorage.clear(); });

  it('requests the converter /thumbnail (not /download) for a non-renderable image (.exr)', async () => {
    const items: Item[] = [
      { id: 'X1', name: 'render.exr', cog_type: 'asset_file', parent_id: null },
      { id: 'X2', name: 'okimage.png', cog_type: 'asset_file', parent_id: null },
    ];
    const { calls } = mockFetch(items);

    render(<FileBrowser isOpen onClose={() => {}} accessToken="tok" onFileSelect={() => {}} allowMultiple />);

    // Grid filenames carry zero-width spaces; match on normalized text.
    await screen.findByText((_c, el) => norm(el?.textContent || '') === 'render.exr' && !!el && el.children.length === 0, { timeout: 20000 });

    // exr has a server-side converter (hdr-converter) whose PNG is served by
    // /thumbnail — so the picker asks for the CONVERTER preview, and never fires
    // a doomed /download for the raw (unpaintable) .exr bytes.
    await waitFor(() => expect(calls.some(c => c.includes('X1/thumbnail'))).toBe(true), { timeout: 20000 });
    expect(calls.some(c => c.includes('X1/download'))).toBe(false);

    // With the mocked /thumbnail returning not-ok (no converted preview yet),
    // the exr tile falls back to its labeled extension placeholder.
    expect(screen.getByText('exr')).toBeTruthy();
  });
});
