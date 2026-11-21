/**
 * sz-4.9c — the node config panel's OAuth-connection picker.
 *
 * A slot in data.custom_metadata.accepted_secrets that declares
 * `oauth_providers` renders OAuthConnectionPicker instead of the named-secret
 * dropdown. The picker lists GET {api}/integrations/oauth/connections (Bearer)
 * filtered to provider ∈ providers: ACTIVE ones are selectable ("<provider> ·
 * <external_account_id || #id>"), non-active ones appear disabled with
 * "(reconnect needed)". Selecting stores {env_var, oauth_connection_id} in
 * secret_refs via updateNodeData (dropping any secret_name ref for the same
 * env_var); "None" removes it. 403 → admin hint (no throw); empty → connect
 * hint. The token itself is never fetched or rendered client-side.
 */

import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

beforeAll(() => {
  (global as any).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  if (!Element.prototype.hasPointerCapture) Element.prototype.hasPointerCapture = () => false;
  if (!Element.prototype.releasePointerCapture) Element.prototype.releasePointerCapture = () => {};
  if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {};
});

// Mock ONLY authenticatedFetch (managed-stacks poller); keep the rest real.
jest.mock("@/lib/api-client", () => ({
  ...jest.requireActual("@/lib/api-client"),
  authenticatedFetch: jest.fn(),
}));

import { authenticatedFetch } from "@/lib/api-client";
import NodeConfigPanel from "@/components/node-config-panel";

const mockAuthFetch = authenticatedFetch as jest.MockedFunction<typeof authenticatedFetch>;

const CONNECTIONS = [
  { id: 12, provider: "google", external_account_id: "guser@example.com", scopes: [], status: "active" },
  { id: 13, provider: "google", external_account_id: null, scopes: [], status: "expired" },
  { id: 14, provider: "stripe", external_account_id: "suser", scopes: [], status: "active" },
];

function makeResponse(body: any): any {
  return { ok: true, status: 200, headers: new Headers(), json: async () => body };
}

// Route the panel's raw fetches: secrets list (empty) + oauth connections.
function routeFetch(url: string) {
  if (url.includes("/integrations/oauth/connections")) return makeResponse(CONNECTIONS);
  return makeResponse([]);
}

afterEach(() => {
  jest.clearAllMocks();
  delete (global as any).fetch;
});

const slots = [
  { env_var: "OAUTH_ACCESS_TOKEN", description: "Google access token", required: true, oauth_providers: ["google"] },
  { env_var: "NIM_API_KEY", description: "Named secret slot" },
];

const makeNode = (data: any = {}) => ({
  id: "n1",
  type: "process",
  position: { x: 0, y: 0 },
  data: {
    label: "Resize",
    node_kind: "resize",
    custom_metadata: { accepted_secrets: slots },
    ...data,
  },
});

function openSecretsSection() {
  render(
    <NodeConfigPanel
      node={makeNode() as any}
      updateNodeData={jest.fn()}
      onClose={() => {}}
      accessToken="tok"
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: /Secrets/ }));
}

describe("NodeConfigPanel — OAuth connection picker (sz-4.9c)", () => {
  it("renders the picker only for oauth slots; non-oauth slot keeps the named-secret dropdown", async () => {
    (global as any).fetch = jest.fn((url: string) => routeFetch(url as string));
    mockAuthFetch.mockResolvedValue(makeResponse([]));
    openSecretsSection();
    const picker = await screen.findByTestId("oauth-connection-select-OAUTH_ACCESS_TOKEN");
    expect(picker).toBeTruthy();
    // Regression: the plain slot still shows the named-secret dropdown.
    expect(screen.getByText("NIM_API_KEY")).toBeTruthy();
    expect(screen.queryByTestId("oauth-connection-select-NIM_API_KEY")).toBeNull();
  });

  it("lists only active connections whose provider is declared; labels provider · account", async () => {
    (global as any).fetch = jest.fn((url: string) => routeFetch(url as string));
    mockAuthFetch.mockResolvedValue(makeResponse([]));
    openSecretsSection();
    const select = await screen.findByTestId("oauth-connection-select-OAUTH_ACCESS_TOKEN");
    await waitFor(() =>
      expect(screen.getByRole("option", { name: "google · guser@example.com" })).toBeTruthy(),
    );
    expect(screen.getByRole("option", { name: "None" })).toBeTruthy();
    // Non-active google connection is NOT a selectable option...
    expect(screen.queryByRole("option", { name: /reconnect needed/ })).toBeNull();
    // ...and the other provider never appears at all.
    expect(screen.queryByText(/stripe/)).toBeNull();
  });

  it("lists non-active connections disabled with '(reconnect needed)'", async () => {
    (global as any).fetch = jest.fn((url: string) => routeFetch(url as string));
    mockAuthFetch.mockResolvedValue(makeResponse([]));
    openSecretsSection();
    // id 13 has no external_account_id → "#13"; wait for the fetch to land.
    expect(await screen.findByText(/google · #13 \(reconnect needed\)/)).toBeTruthy();
  });

  it("selecting writes {env_var, oauth_connection_id} and drops the secret_name ref for that env_var", async () => {
    (global as any).fetch = jest.fn((url: string) => routeFetch(url as string));
    mockAuthFetch.mockResolvedValue(makeResponse([]));
    const updateNodeData = jest.fn();
    render(
      <NodeConfigPanel
        node={makeNode({
          secret_refs: [{ secret_name: "old-key", env_var: "OAUTH_ACCESS_TOKEN" }],
        }) as any}
        updateNodeData={updateNodeData}
        onClose={() => {}}
        accessToken="tok"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Secrets/ }));
    const select = await screen.findByTestId("oauth-connection-select-OAUTH_ACCESS_TOKEN");
    // Wait until the connections fetch landed so option "12" exists.
    await waitFor(() =>
      expect(screen.getByRole("option", { name: "google · guser@example.com" })).toBeTruthy(),
    );
    fireEvent.change(select, { target: { value: "12" } });
    expect(updateNodeData).toHaveBeenCalledWith(
      "n1",
      expect.objectContaining({
        secret_refs: [expect.objectContaining({ env_var: "OAUTH_ACCESS_TOKEN", oauth_connection_id: 12 })],
      }),
    );
    const [, payload] = updateNodeData.mock.calls[0];
    expect(payload.secret_refs.some((r: any) => r.secret_name)).toBe(false);
  });

  it("'None' removes the oauth ref for that env_var", async () => {
    (global as any).fetch = jest.fn((url: string) => routeFetch(url as string));
    mockAuthFetch.mockResolvedValue(makeResponse([]));
    const updateNodeData = jest.fn();
    render(
      <NodeConfigPanel
        node={makeNode({
          secret_refs: [{ env_var: "OAUTH_ACCESS_TOKEN", oauth_connection_id: 12 }],
        }) as any}
        updateNodeData={updateNodeData}
        onClose={() => {}}
        accessToken="tok"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Secrets/ }));
    const select = await screen.findByTestId("oauth-connection-select-OAUTH_ACCESS_TOKEN");
    fireEvent.change(select, { target: { value: "" } });
    expect(updateNodeData).toHaveBeenCalledTimes(1);
    const [, payload] = updateNodeData.mock.calls[0];
    expect(payload.secret_refs).toEqual([]);
  });

  it("403 shows the admin hint, no throw, no retry loop", async () => {
    (global as any).fetch = jest.fn((url: string) =>
      url.includes("/integrations/oauth/connections")
        ? { ok: false, status: 403, json: async () => ({}) }
        : makeResponse([]),
    );
    mockAuthFetch.mockResolvedValue(makeResponse([]));
    openSecretsSection();
    expect(
      await screen.findByText("Ask a tenant admin to connect google in Settings → Integrations"),
    ).toBeTruthy();
    // Exactly one connections fetch — the hint does not re-poll.
    const oauthCalls = ((global as any).fetch as jest.Mock).mock.calls.filter((c: any[]) =>
      String(c[0]).includes("/integrations/oauth/connections"),
    );
    expect(oauthCalls.length).toBe(1);
  });

  it("empty connection list shows the 'Connect <provider> in Settings → Integrations' hint", async () => {
    (global as any).fetch = jest.fn((url: string) =>
      url.includes("/integrations/oauth/connections") ? makeResponse([]) : makeResponse([]),
    );
    mockAuthFetch.mockResolvedValue(makeResponse([]));
    openSecretsSection();
    expect(
      await screen.findByText("Connect google in Settings → Integrations"),
    ).toBeTruthy();
  });

  it("badge counts an oauth-bound slot as bound", async () => {
    (global as any).fetch = jest.fn((url: string) => routeFetch(url as string));
    mockAuthFetch.mockResolvedValue(makeResponse([]));
    render(
      <NodeConfigPanel
        node={makeNode({ secret_refs: [{ env_var: "OAUTH_ACCESS_TOKEN", oauth_connection_id: 12 }] }) as any}
        updateNodeData={jest.fn()}
        onClose={() => {}}
        accessToken="tok"
      />,
    );
    expect(screen.getByText("1/2")).toBeTruthy();
  });

  it("no token / Authorization value is rendered anywhere", () => {
    (global as any).fetch = jest.fn((url: string) => routeFetch(url as string));
    mockAuthFetch.mockResolvedValue(makeResponse([]));
    openSecretsSection();
    const text = document.body.textContent;
    expect(text).not.toContain("Bearer");
    expect(text).not.toContain("access_token");
    expect(text).not.toContain("encrypted_access_token");
    expect(/Bearer|eyJ|secret-token/.test(text)).toBe(false);
  });
});
