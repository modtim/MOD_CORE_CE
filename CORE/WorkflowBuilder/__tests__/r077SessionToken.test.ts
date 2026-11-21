/**
 * GSEC R-077 / Q27=A — WB access/refresh tokens must not persist in
 * localStorage.
 *
 * Covers:
 *   1. storage location   — tokens read/write via sessionStorage, not localStorage
 *   2. silent re-auth     — a fresh tab (no token) issues a prompt=none PKCE
 *                           auth request before any login screen
 *   3. logout broadcast   — a logout reaches sibling tabs (BroadcastChannel +
 *                           a storage-event fallback) and clears tokens
 *   4. one-time migration — leftover localStorage token keys are DELETED
 *                           (never copied) on load
 */

import {
  getAccessToken,
  getRefreshToken,
  saveWorkflowTokens,
  clearWorkflowTokens,
  migrateLegacyLocalStorageTokens,
  broadcastLogout,
  tryRefreshToken,
  ACCESS_TOKEN_KEY,
  REFRESH_TOKEN_KEY,
  TOKEN_CLIENT_KEY,
} from '@/lib/api-client';
import {
  buildSilentSsoAuthUrl,
  SSO_CLIENT_ID,
  SSO_CALLBACK_PATH,
} from '@/lib/silent-sso';

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

// ---------------------------------------------------------------------------
// 1. Storage location
// ---------------------------------------------------------------------------
describe('R-077 token storage location (sessionStorage, not localStorage)', () => {
  it('getAccessToken reads from sessionStorage', () => {
    sessionStorage.setItem(ACCESS_TOKEN_KEY, 'sess-token');
    localStorage.setItem(ACCESS_TOKEN_KEY, 'local-token');
    expect(getAccessToken()).toBe('sess-token');
  });

  it('getAccessToken is null when nothing is in sessionStorage (even if localStorage has one)', () => {
    localStorage.setItem(ACCESS_TOKEN_KEY, 'stale-local');
    expect(getAccessToken()).toBeNull();
  });

  it('saveWorkflowTokens writes to sessionStorage only', () => {
    saveWorkflowTokens('a', 'r', 'dashboard-client');
    expect(sessionStorage.getItem(ACCESS_TOKEN_KEY)).toBe('a');
    expect(sessionStorage.getItem(REFRESH_TOKEN_KEY)).toBe('r');
    expect(sessionStorage.getItem(TOKEN_CLIENT_KEY)).toBe('dashboard-client');
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
    expect(localStorage.getItem(REFRESH_TOKEN_KEY)).toBeNull();
  });

  it('clearWorkflowTokens removes the sessionStorage tokens', () => {
    saveWorkflowTokens('a', 'r');
    clearWorkflowTokens();
    expect(getAccessToken()).toBeNull();
    expect(getRefreshToken()).toBeNull();
  });

  it('uses the key names the components used to hardcode', () => {
    expect(ACCESS_TOKEN_KEY).toBe('workflow_access_token');
    expect(REFRESH_TOKEN_KEY).toBe('workflow_refresh_token');
  });
});

// ---------------------------------------------------------------------------
// 2. Silent re-auth (prompt=none PKCE)
// ---------------------------------------------------------------------------
describe('R-077 silent SSO re-auth', () => {
  it('builds a prompt=none auth URL for the workflow-builder-client with PKCE', () => {
    const url = buildSilentSsoAuthUrl(
      'https://kc/realms/modtex/protocol/openid-connect/auth',
      'https://wb.example.com/auth/callback',
      'challenge123',
    );
    expect(url).toContain('client_id=workflow-builder-client');
    expect(url).toContain(encodeURIComponent('https://wb.example.com/auth/callback'));
    expect(url).toContain('prompt=none');
    expect(url).toContain('code_challenge=challenge123');
    expect(url).toContain('code_challenge_method=S256');
    expect(url).toContain('response_type=code');
    // 2400LR-b1j: offline_access would end the KC SSO session at code->token.
    expect(url).not.toContain('offline_access');
  });

  it('targets the dedicated /auth/callback path', () => {
    expect(SSO_CALLBACK_PATH).toBe('/auth/callback');
    expect(SSO_CLIENT_ID).toBe('workflow-builder-client');
  });

  it('silent-SSO mint records the workflow-builder-client as the token client', () => {
    // The callback page calls saveWorkflowTokens with the SSO client; a later
    // refresh must go to Keycloak directly (public client), not the BFF.
    saveWorkflowTokens('a', 'r', SSO_CLIENT_ID);
    // getTokenClient is exercised indirectly via tryRefreshToken below.
    expect(sessionStorage.getItem(TOKEN_CLIENT_KEY)).toBe(SSO_CLIENT_ID);
  });
});

// ---------------------------------------------------------------------------
// 3. Logout broadcast
// ---------------------------------------------------------------------------
describe('R-077 cross-tab logout broadcast', () => {
  let posted: unknown[];
  let realBC: unknown;

  // Inject a BroadcastChannel that records postMessage, then load a FRESH
  // copy of api-client so its module-level channel singleton binds to the
  // mock (jsdom has no native BroadcastChannel).
  beforeEach(() => {
    posted = [];
    realBC = (global as any).BroadcastChannel;
    (global as any).BroadcastChannel = class {
      constructor(_name: string) {}
      postMessage(m: unknown) {
        posted.push(m);
      }
      close() {}
    };
  });

  afterEach(() => {
    (global as any).BroadcastChannel = realBC;
    jest.resetModules();
  });

  it('broadcastLogout posts {type:logout} on the channel AND writes a storage sentinel', () => {
    let broadcast: () => void;
    jest.isolateModules(() => {
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      broadcast = require('@/lib/api-client').broadcastLogout;
    });
    broadcast!();
    expect(posted).toEqual([expect.objectContaining({ type: 'logout' })]);
    // Fallback sentinel for browsers without BroadcastChannel.
    expect(localStorage.getItem('mod_wb_logout_sentinel')).toBeTruthy();
  });

  it('broadcastLogout never throws even with no channel and storage disabled', () => {
    const realRemoveItem = localStorage.removeItem.bind(localStorage);
    Object.defineProperty(localStorage, 'removeItem', {
      value: () => {
        throw new Error('storage unavailable');
      },
      configurable: true,
    });
    // Force the no-BroadcastChannel branch by clearing the global.
    (global as any).BroadcastChannel = undefined;
    try {
      jest.isolateModules(() => {
        // eslint-disable-next-line @typescript-eslint/no-var-requires
        const b = require('@/lib/api-client').broadcastLogout as () => void;
        expect(() => b()).not.toThrow();
      });
    } finally {
      Object.defineProperty(localStorage, 'removeItem', {
        value: realRemoveItem,
        configurable: true,
      });
    }
  });
});

// ---------------------------------------------------------------------------
// 4. One-time migration
// ---------------------------------------------------------------------------
describe('R-077 one-time localStorage migration', () => {
  it('deletes leftover token keys from localStorage WITHOUT copying them', () => {
    localStorage.setItem(ACCESS_TOKEN_KEY, 'legacy-access');
    localStorage.setItem(REFRESH_TOKEN_KEY, 'legacy-refresh');
    migrateLegacyLocalStorageTokens();
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
    expect(localStorage.getItem(REFRESH_TOKEN_KEY)).toBeNull();
    // ...and nothing is copied into sessionStorage.
    expect(sessionStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
    expect(sessionStorage.getItem(REFRESH_TOKEN_KEY)).toBeNull();
    expect(getAccessToken()).toBeNull();
  });

  it('is idempotent (safe to call repeatedly)', () => {
    localStorage.setItem(ACCESS_TOKEN_KEY, 'x');
    migrateLegacyLocalStorageTokens();
    migrateLegacyLocalStorageTokens();
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Bonus: refresh routes by the minting client
// ---------------------------------------------------------------------------
describe('tryRefreshToken routes by minting client', () => {
  const realFetch = global.fetch;
  afterEach(() => {
    global.fetch = realFetch;
    localStorage.clear();
    sessionStorage.clear();
    jest.restoreAllMocks();
  });

  it('uses the BFF for a dashboard-client (confidential) token', async () => {
    sessionStorage.setItem(REFRESH_TOKEN_KEY, 'rt');
    // dashboard-client is the default — no TOKEN_CLIENT_KEY set.
    const calls: string[] = [];
    global.fetch = jest.fn().mockImplementation(async (url: string) => {
      calls.push(String(url));
      return { ok: true, json: async () => ({ access_token: 'na', refresh_token: 'nr' }) };
    }) as unknown as typeof fetch;

    const res = await tryRefreshToken();
    expect(res).toBe('na');
    // BFF path hits /auth/refresh-token (never the raw Keycloak token endpoint).
    expect(calls.some((u) => u.includes('/auth/refresh-token'))).toBe(true);
    expect(calls.some((u) => u.endsWith('/token'))).toBe(false);
  });

  it('uses Keycloak directly for a public-client (workflow-builder) token', async () => {
    sessionStorage.setItem(REFRESH_TOKEN_KEY, 'rt');
    sessionStorage.setItem(TOKEN_CLIENT_KEY, 'workflow-builder-client');
    const calls: string[] = [];
    global.fetch = jest
      .fn()
      .mockImplementation(async (url: string) => {
        calls.push(String(url));
        if (String(url).includes('/auth/discovery')) {
          return { ok: true, json: async () => ({ token_endpoint: 'https://kc/token' }) };
        }
        return { ok: true, json: async () => ({ access_token: 'pub-na', refresh_token: 'pub-nr' }) };
      }) as unknown as typeof fetch;

    const res = await tryRefreshToken();
    expect(res).toBe('pub-na');
    // Discovery then a direct POST to the Keycloak token endpoint.
    expect(calls.some((u) => u.includes('/auth/discovery'))).toBe(true);
    expect(calls).toContain('https://kc/token');
    // And it did NOT use the BFF.
    expect(calls.some((u) => u.includes('/auth/refresh-token'))).toBe(false);
  });
});
