/**
 * node-builder audit F4 + F5 (AUDIT-CE-JOURNEY-NODE-BUILDER-FIRST-USE-FRESH-ADMIN):
 *
 *  F4 (components/node-library.tsx, empty-state branch): an empty library used
 *      to say only "No nodes found." — no next step. It now names the fix:
 *      "Nodes are loaded by the API at start-up: reload the page; if it stays
 *      empty, check the API container logs." (edition-neutral text).
 *  F5 (the /nodes/ fetch failure handler): a 401/403 is an EXPIRED LOGIN, not a
 *      degraded backend. It must toast "Your session expired - sign in again"
 *      carrying the existing "Sign in again" action (lib/sign-in-again.ts),
 *      must NOT consume the 3-retry budget, and must NOT toast "Backend
 *      unavailable". Every other status keeps the old degraded/retry path.
 *
 * Both layers are pinned by RENDERING the real component (the established
 * pattern in this dir — see nodeLibraryFunctionsInScroll.test.tsx: jsdom has
 * no fetch, so the palette GET is handed a canned response).
 */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import NodeLibrary from '@/components/node-library';

const toastMock = jest.fn();
jest.mock('@/components/ui/use-toast', () => ({ toast: (...args: unknown[]) => toastMock(...args) }));

const signInAgainMock = jest.fn();
jest.mock('@/lib/sign-in-again', () => ({
  startSignInAgain: (...args: unknown[]) => signInAgainMock(...args),
}));

const HINT = 'Nodes are loaded by the API at start-up';
const SESSION_TITLE = 'Your session expired - sign in again';
const BACKEND_TITLE = 'Backend unavailable';

const canned = (status: number, ok: boolean, body: any[]) => () =>
  Promise.resolve({ ok, status, json: () => Promise.resolve(body), text: async () => '[]' });

const restoreFetch = (resp: () => Promise<any>) => {
  const origFetch = (global as any).fetch;
  (global as any).fetch = resp;
  return () => ((global as any).fetch = origFetch);
};

// The empty path burns the component's own 1s retry ticks; 6s covers them
// (jest.setup.ts raises asyncUtilTimeout to 20s, testTimeout is 30s).
async function flush(): Promise<void> {
  for (let i = 0; i < 12; i += 1) await new Promise((r) => setTimeout(r, 500));
}

beforeEach(() => {
  toastMock.mockReset();
  signInAgainMock.mockReset();
});

describe('node-library empty state names a next step (F4)', () => {
  it('empty library renders the hint sentence, not a bare "No nodes found."', async () => {
    const restore = restoreFetch(canned(200, true, []));
    try {
      const { container } = render(<NodeLibrary accessToken="tok" hasLoadedBefore={true} />);
      await flush();
      // The empty branch is the filtered-empty palette: a search that matches
      // nothing (same driver marketplaceCurate.test.tsx uses).
      const inputEl = container.querySelector('input');
      expect(inputEl).not.toBeNull();
      fireEvent.change(inputEl!, { target: { value: 'zzz-nothing' } });
      const p = screen.getByText('No nodes found.', { exact: false });
      expect(p.textContent).toContain('No nodes found.');
      expect(p.textContent).toContain(HINT);
    } finally {
      restore();
    }
  });
});

describe('node-library 401/403 is an expired login, not a degraded backend (F5)', () => {
  it('401 -> session-expired toast + the existing Sign in again action', async () => {
    window.history.replaceState({}, "", "/?workflow=42");
    const restore = restoreFetch(canned(401, false, []));
    try {
      render(<NodeLibrary accessToken="tok" hasLoadedBefore={false} />);
      await flush();
      const calls = toastMock.mock.calls.map((c) => c[0] as any);
      expect(calls.some((t) => t.title === SESSION_TITLE)).toBe(true);
      expect(calls.every((t) => t.title !== BACKEND_TITLE)).toBe(true);
      expect(signInAgainMock).toHaveBeenCalledWith(42);
    } finally {
      restore();
    }
  });

  it('403 behaves like 401', async () => {
    window.history.replaceState({}, "", "/?workflow=7");
    const restore = restoreFetch(canned(403, false, []));
    try {
      render(<NodeLibrary accessToken="tok" hasLoadedBefore={false} />);
      await flush();
      const calls = toastMock.mock.calls.map((c) => c[0] as any);
      expect(calls.some((t) => t.title === SESSION_TITLE)).toBe(true);
      expect(calls.every((t) => t.title !== BACKEND_TITLE)).toBe(true);
      expect(signInAgainMock).toHaveBeenCalledWith(7);
    } finally {
      restore();
    }
  });

  it('503 keeps the old behaviour: degraded + "Backend unavailable", no sign-in toast', async () => {
    window.history.replaceState({}, "", "/");
    const restore = restoreFetch(canned(503, false, []));
    try {
      render(<NodeLibrary accessToken="tok" hasLoadedBefore={false} />);
      await flush();
      const calls = toastMock.mock.calls.map((c) => c[0] as any);
      expect(calls.some((t) => t.title === BACKEND_TITLE)).toBe(true);
      expect(calls.every((t) => t.title !== SESSION_TITLE)).toBe(true);
      expect(signInAgainMock).not.toHaveBeenCalled();
    } finally {
      restore();
    }
  });
});
