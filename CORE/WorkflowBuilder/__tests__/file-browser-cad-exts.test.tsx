/**
 * FB-CAD-EXTS — the WB file-browser thumbnail path must request a server
 * thumbnail for STEP/STP CAD ONLY, never for IGES/IGS
 * (FIX-CAD-IGES-COMPLETE / the FIX-IGES-FOLLOWUPS CAD_THUMB_EXTS /
 * CAD_PREVIEW_EXTS split).
 *
 * The 3d-thumbnail-gen worker reads STEP/STP via cadquery importStep; there is
 * no IGES reader, so an IGES thumbnail task is a doomed failing task. IGES/IGS
 * stay recognised as CAD (labelled tile; the Dashboard preview still opens them
 * client-side through the real OCCT parser — untouched).
 *
 * Drives FileThumbnail (the component that owns the fetch) directly: one
 * component per file name, fetch stubbed globally.
 * Proved red via MCW_OVERRIDE: restoring iges/igs into CAD_THUMB_EXTS makes
 * the .iges tile fire a /thumbnail fetch -> the "never requests" expect fails.
 */

import React from 'react';
import { render, screen, cleanup } from '@testing-library/react';
import { FileThumbnail } from '@/components/file-browser';

const settle = () => new Promise((r) => setTimeout(r, 0));

function fakeFetch() {
  const calls: string[] = [];
  const fn = jest.fn((url: RequestInfo | URL) => {
    calls.push(String(url));
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ status: 'unavailable', url: null }) });
  });
  (global as any).fetch = fn;
  return { fn, calls };
}

const thumbCallsOf = (calls: string[]) => calls.filter((c) => c.includes('/thumbnail'));

describe('FB-CAD-EXTS — CAD thumbnail split', () => {
  beforeEach(() => { window.localStorage.setItem('wfbViewMode', 'list'); });
  afterEach(() => { cleanup(); jest.restoreAllMocks(); window.localStorage.clear(); (global as any).fetch = undefined; });

  it('a .step tile requests GET /assets/{id}/thumbnail', async () => {
    const { calls } = fakeFetch();
    render(<FileThumbnail item={{ id: 'cog-step', name: 'part.step', cog_type: 'asset_file' }} accessToken="tok" />);
    await screen.findByText(/step/);
    await settle(); await settle();

    const thumbs = thumbCallsOf(calls);
    expect(thumbs).toHaveLength(1);
    expect(thumbs[0]).toContain('cog-step');
    expect(thumbs[0]).toContain('/thumbnail');
  });

  it('a .stp tile requests GET /assets/{id}/thumbnail', async () => {
    const { calls } = fakeFetch();
    render(<FileThumbnail item={{ id: 'cog-stp', name: 'part.stp', cog_type: 'asset_file' }} accessToken="tok" />);
    await screen.findByText(/stp/);
    await settle(); await settle();

    const thumbs = thumbCallsOf(calls);
    expect(thumbs).toHaveLength(1);
    expect(thumbs[0]).toContain('cog-stp');
  });

  it('a .iges tile NEVER requests a thumbnail (preview-only CAD)', async () => {
    const { calls } = fakeFetch();
    render(<FileThumbnail item={{ id: 'cog-iges', name: 'part.iges', cog_type: 'asset_file' }} accessToken="tok" />);
    await screen.findByText(/iges/);
    await settle(); await settle();

    expect(thumbCallsOf(calls)).toHaveLength(0);
  });

  it('a .igs tile NEVER requests a thumbnail (preview-only CAD)', async () => {
    const { calls } = fakeFetch();
    render(<FileThumbnail item={{ id: 'cog-igs', name: 'part.igs', cog_type: 'asset_file' }} accessToken="tok" />);
    await screen.findByText(/igs/);
    await settle(); await settle();

    expect(thumbCallsOf(calls)).toHaveLength(0);
  });
});
