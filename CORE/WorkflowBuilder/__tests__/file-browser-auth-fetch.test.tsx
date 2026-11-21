/**
 * FB-AUTH-FETCH — the WB file browser must route its golden-path API calls
 * through the builder's authenticatedFetch so a 401 refreshes once and retries
 * ONCE with the new token (FIX-1 FILEBROWSER-USE-AUTHENTICATED-FETCH-D72).
 *
 * Owner symptom (Dell, results-1110/DIAG-DELL-BUILDER-401-ON-ASSETS-AND-STACKS-D72):
 * "New Folder" 401'ed and the picker could not add a file, because file-browser.tsx
 * sent a raw fetch/XHR with the in-memory accessToken prop and never refreshed —
 * the WB access token lives 5 minutes and the hand-off token arrives already old.
 *
 * RED before the fix (proved via MCW_OVERRIDE of the old file): the raw fetch
 * returns the 401, no refresh happens, the listing stays empty and the create
 * dialog stays open. GREEN after: exactly one retry carrying the NEW token.
 */

import React from 'react';
import { render, screen, cleanup, act, fireEvent } from '@testing-library/react';
import { FileBrowser } from '@/components/file-browser';
import { looksExpired } from '@/components/file-browser';

const API = 'http://api.localhost'; // apiUrl() default in jsdom (lib/runtime-config.ts)
const REFRESH_URL = `${API}/auth/refresh-token`;
const LIST_URL = `${API}/assets/`;
const OLD_TOKEN = 'stale-token';
const NEW_TOKEN = 'fresh-token';

// A JWT-shaped token whose exp is 10 s away (expired-enough for the upload
// pre-refresh) vs one 10 minutes away.
const b64 = (o: any) => Buffer.from(JSON.stringify(o)).toString('base64');
const EXPIRED_JWT = `h.${b64({ exp: Math.floor(Date.now() / 1000) + 10 })}.s`;
const FRESH_JWT = `h.${b64({ exp: Math.floor(Date.now() / 1000) + 600 })}.s`;

const makeChildren = (n: number) =>
  Array.from({ length: n }, (_, i) => ({
    id: `id-${i}`,
    name: `file-${i}.png`,
    cog_type: 'asset_file',
    parent_id: null,
  }));

const settle = () => new Promise((r) => setTimeout(r, 0));

// authenticatedFetch normalises headers into a `Headers` instance, so the stub
// must read Authorization from a Headers object OR a plain object.
const authOf = (opts: any): string => {
  const h = opts?.headers;
  if (!h) return '';
  try {
    const v = (h as any).get?.('Authorization');
    if (v) return String(v);
  } catch { /* not a Headers instance */ }
  if (typeof h === 'object') {
    const plain = (h.Authorization ?? h.authorization ?? '');
    return String(plain);
  }
  return '';
};

// fetch stub: 401 on the OLD token for the golden-path calls (list + create
// folder), serves the BFF refresh endpoint, and answers 200/201 for the NEW
// token. Every call is recorded so the retry is countable.
function authFetchStub(listBody: any, postBody: any) {
  const calls: { url: string; auth: string; method: string }[] = [];
  const fn = jest.fn((url: RequestInfo | URL, opts: any) => {
    const u = String(url);
    const auth = authOf(opts);
    const m = String(opts?.method ?? 'GET');
    calls.push({ url: u, auth, method: m });

    if (u === REFRESH_URL) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ access_token: NEW_TOKEN, refresh_token: 'rt-2' }),
      });
    }
    const golden = u.startsWith(LIST_URL) || (u === API + '/assets/' && m === 'POST');
    if (golden && auth === `Bearer ${OLD_TOKEN}`) {
      return Promise.resolve({
        ok: false,
        status: 401,
        json: () => Promise.resolve({ detail: 'access token expired' }),
      });
    }
    if (u.startsWith(LIST_URL)) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve(listBody) });
    }
    return Promise.resolve({ ok: true, status: 201, json: () => Promise.resolve(postBody) });
  });
  (global as any).fetch = fn;
  return { fn, calls };
}

const assetCallsOf = (calls: typeof undefined) =>
  (calls as any[]).filter((c: any) => c.url.startsWith(LIST_URL));

describe('FB-AUTH-FETCH — file browser refreshes and retries on 401', () => {
  beforeEach(() => {
    window.localStorage.setItem('wfbViewMode', 'list');
    // The silent-refresh path needs a refresh token in sessionStorage.
    sessionStorage.setItem('workflow_refresh_token', 'rt-1');
  });
  afterEach(() => {
    cleanup();
    jest.restoreAllMocks();
    window.localStorage.clear();
    sessionStorage.clear();
    (global as any).fetch = undefined;
  });

  // --- list: 401 -> refresh -> 200, retried exactly once with the new token --
  it('a 401 on GET /assets/ is retried once with the refreshed token and the rows paint', async () => {
    const { calls } = authFetchStub(makeChildren(3), 'GET');
    render(<FileBrowser isOpen onClose={() => {}} accessToken={OLD_TOKEN} />);

    await settle(); await settle(); await settle();

    const assetCalls = assetCallsOf(calls);
    expect(assetCalls).toHaveLength(2); // one try + exactly one retry
    expect(assetCalls[0].auth).toBe(`Bearer ${OLD_TOKEN}`);
    expect(assetCalls[1].auth).toBe(`Bearer ${NEW_TOKEN}`);
    expect(calls.filter((c) => c.url === REFRESH_URL)).toHaveLength(1);
    // The retry proves it: the rows exist only after the 200.
    expect(screen.queryAllByText(/^file-\d\.png$/)).toHaveLength(3);
  });

  // --- New Folder: 401 -> refresh -> 201, dialog closes (no error text) ------
  it('New Folder 401 refreshes and retries once, closing the dialog instead of erroring', async () => {
    const { calls } = authFetchStub([], { id: '9' });
    render(<FileBrowser isOpen onClose={() => {}} accessToken={OLD_TOKEN} />);
    await settle();

    await act(async () => {
      screen.getByRole('button', { name: /New Folder/ }).click();
    });
    await settle();

    const nameInput = screen.getByRole('textbox');
    fireEvent.change(nameInput, { target: { value: 'docs' } });
    await settle();

    await act(async () => {
      screen.getByRole('button', { name: 'Create' }).click();
    });
    await settle(); await settle(); await settle();

    const posts = calls.filter((c) => c.method === 'POST' && c.url === API + '/assets/');
    expect(posts).toHaveLength(2);
    expect(posts[0].auth).toBe(`Bearer ${OLD_TOKEN}`);
    expect(posts[1].auth).toBe(`Bearer ${NEW_TOKEN}`);
    // Success closes the dialog; the old code left it open and painted the 401
    // detail as an upload error.
    expect(screen.queryByText('Create New Folder')).toBeNull();
    expect(screen.queryByText(/access token expired/)).toBeNull();
  });

  // --- the disabled chooser says WHY (no folder open) ------------------------
  it('a disabled chooser (no folder open) shows the hint text and the title', async () => {
    const { calls } = authFetchStub(makeChildren(2), 'POST');
    render(<FileBrowser isOpen onClose={() => {}} accessToken={OLD_TOKEN} />);
    await settle(); await settle();

    // No folder is open (currentFolder stays null) -> the helper line renders and
    // the Upload button carries the reason as its title. The old file had neither.
    expect(screen.queryByText(/Open or create a folder first\./)).toBeTruthy();
    const uploadBtn = Array.from(document.querySelectorAll('button')).find(
      (b) => (b.textContent || '').includes('Upload')
    );
    expect(uploadBtn).toBeTruthy();
    expect(uploadBtn!.getAttribute('title')).toBe('Open or create a folder first.');
    // The button is still disabled while no folder is open.
    expect(uploadBtn!.hasAttribute('disabled')).toBe(true);
  });

  // --- upload pre-refresh decision (XHR cannot go through authenticatedFetch) -
  it('the upload pre-refresh fires only when the JWT exp is inside 60 s', () => {
    expect(looksExpired(EXPIRED_JWT)).toBe(true);
    expect(looksExpired(FRESH_JWT)).toBe(false);
    expect(looksExpired(null)).toBe(false);
    expect(looksExpired('not-a-jwt')).toBe(false);
  });
});
