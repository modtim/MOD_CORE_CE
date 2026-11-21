/**
 * J159b — the WB hook's silent token refresh must use the client that
 * MINTED the held token, never a hardcoded dashboard-client.
 *
 * The hook (hooks/useWorkflowApi.ts) used to post /auth/refresh-token with
 * `client_id: "dashboard-client"` for whatever refresh token the tab held —
 * including workflow-builder-client tokens from the silent-SSO path
 * (lib/silent-sso.ts), whose refresh tokens the BFF cannot honor. Keycloak
 * refresh tokens are client-bound, so that refresh either failed (hard
 * bounce) or, worse, minted a token under the wrong client.
 *
 * Fix: the hook now delegates to the shared api-client tryRefreshToken,
 * which routes by getTokenClient(): dashboard-client -> BFF
 * (POST `${API_URL}/auth/refresh-token`), anything else -> Keycloak-direct
 * (form-encoded POST to the discovery token_endpoint).
 *
 * Pinned here (real hook under renderHook; only global.fetch is mocked):
 *   - workflow-builder-client token + 401: NO BFF refresh call at all, and
 *     no request body anywhere names the dashboard client; the refresh goes
 *     to the Keycloak token endpoint carrying the WB client;
 *   - dashboard-client token + 401: unchanged behaviour — exactly one BFF
 *     refresh carrying that client, retry succeeds, no bounce.
 */

import { act, renderHook } from "@testing-library/react";
import { useWorkflowApi } from "@/hooks/useWorkflowApi";
import {
  ACCESS_TOKEN_KEY,
  REFRESH_TOKEN_KEY,
  TOKEN_CLIENT_KEY,
  DEFAULT_TOKEN_CLIENT,
} from "@/lib/api-client";

let toastTitles: string[] = [];
jest.mock("@/components/ui/use-toast", () => ({ toast: (...args: unknown[]) => {
  toastTitles.push(String((args[0] as any)?.title ?? ""));
} }));

function makeResponse(status: number, body: unknown = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
    text: async () => "",
  } as unknown as Response;
}

const WF = (id: number, name: string) =>
  ({ id, name, description: "", is_system: false, archived_at: null } as any);

const LIST_URL = /\/road\/\?/;
const BFF_REFRESH_URL = /\/auth\/refresh-token$/;
const DISCOVERY_URL = /\/auth\/discovery$/;
const KEYCLOAK_TOKEN_URL = /\/protocol\/openid-connect\/token$/;

async function flush(): Promise<void> {
  for (let i = 0; i < 20; i += 1) await act(async () => { await Promise.resolve(); });
}

interface FetchCall {
  url: string;
  body: string | null;
}

async function mount(): Promise<any> {
  window.history.replaceState({}, "", "/");
  const hook = renderHook(() => useWorkflowApi([], [], jest.fn(), jest.fn(), {} as any));
  await flush();
  return hook;
}

describe("useWorkflowApi hook refresh routes by minting client (J159b)", () => {
  let calls: FetchCall[] = [];

  const record = (url: string, options: RequestInit): FetchCall => ({
    url: String(url),
    body: typeof options.body === "string" ? String(options.body) : null,
  });

  beforeEach(() => {
    toastTitles = [];
    calls = [];
    jest.useFakeTimers();
    sessionStorage.clear();
    let listAttempts = 0;
    global.fetch = (jest.fn(async (url: string, options?: RequestInit) => {
      const u = String(url);
      calls.push(record(u, options ?? ({} as RequestInit)));
      if (LIST_URL.test(u)) {
        // First attempt 401s (expired access token); the retry succeeds.
        listAttempts += 1;
        return listAttempts === 1 ? makeResponse(401) : makeResponse(200, [WF(1, "alpha")]);
      }
      if (BFF_REFRESH_URL.test(u)) {
        return makeResponse(200, { access_token: "fresh-access", refresh_token: "fresh-refresh" });
      }
      if (DISCOVERY_URL.test(u)) {
        return makeResponse(200, {
          token_endpoint: "https://keycloak.example.com/realms/mod/protocol/openid-connect/token",
        });
      }
      if (KEYCLOAK_TOKEN_URL.test(u)) {
        return makeResponse(200, { access_token: "fresh-access", refresh_token: "fresh-refresh" });
      }
      return makeResponse(200, []);
    }) as unknown) as typeof fetch;
  });

  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  const drive401 = async (hook: any) => {
    hook.result.current.searchWorkflows("alpha");
    act(() => {
      jest.advanceTimersByTime(400);
    });
    await flush();
  };

  it("workflow-builder-client token: refresh goes Keycloak-direct, never dashboard-client", async () => {
    sessionStorage.setItem(ACCESS_TOKEN_KEY, "wb-access");
    sessionStorage.setItem(REFRESH_TOKEN_KEY, "wb-refresh");
    sessionStorage.setItem(TOKEN_CLIENT_KEY, "workflow-builder-client");
    const hook = await mount();
    expect(hook.result.current.accessToken).toBe("wb-access");

    await drive401(hook);

    const refreshCalls = calls.filter((c) => BFF_REFRESH_URL.test(c.url) || KEYCLOAK_TOKEN_URL.test(c.url));
    expect(refreshCalls).toHaveLength(1);
    expect(KEYCLOAK_TOKEN_URL.test(refreshCalls[0].url)).toBe(true);
    expect(BFF_REFRESH_URL.test(refreshCalls[0].url)).toBe(false);
    expect(String(refreshCalls[0].body)).toContain("client_id=workflow-builder-client");
    // The whole point of the fix: nothing the hook sends may name the
    // dashboard client.
    const dashboardNamed = calls.filter((c) => String(c.body ?? "").includes(DEFAULT_TOKEN_CLIENT));
    expect(dashboardNamed).toHaveLength(0);
    // The retry went through with the fresh token — no hard bounce.
    expect(hook.result.current.accessToken).toBe("fresh-access");
    expect(toastTitles.includes("Session Expired")).toBe(false);
  });

  it("dashboard-client token: unchanged — one BFF refresh carrying dashboard-client", async () => {
    sessionStorage.setItem(ACCESS_TOKEN_KEY, "dash-access");
    sessionStorage.setItem(REFRESH_TOKEN_KEY, "dash-refresh");
    sessionStorage.setItem(TOKEN_CLIENT_KEY, DEFAULT_TOKEN_CLIENT);
    const hook = await mount();
    expect(hook.result.current.accessToken).toBe("dash-access");

    await drive401(hook);

    const refreshCalls = calls.filter((c) => BFF_REFRESH_URL.test(c.url) || KEYCLOAK_TOKEN_URL.test(c.url));
    expect(refreshCalls).toHaveLength(1);
    expect(BFF_REFRESH_URL.test(refreshCalls[0].url)).toBe(true);
    expect(String(refreshCalls[0].body)).toContain(DEFAULT_TOKEN_CLIENT);
    expect(hook.result.current.accessToken).toBe("fresh-access");
    expect(toastTitles.includes("Session Expired")).toBe(false);
  });
});
