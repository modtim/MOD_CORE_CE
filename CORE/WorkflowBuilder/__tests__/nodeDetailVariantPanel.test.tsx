/**
 * 23.1a.s4 — the node detail page's Variants panel.
 *
 * app/nodes/[id] redirects to /store/node/[id], so the node detail page IS
 * app/store/[kind]/[id]/page.tsx. This test drives that page with kind=node and
 * asserts the panel chain end to end: the verdict list renders its rows, a row's
 * "Edit code" opens the code editor modal for THAT variant, saving re-reads the
 * verdict list (reloadKey bump) and names the saved variant, and the row's
 * "Re-verify this variant only" then POSTs /nodes/42/rebuild {scope:"single_variant"}
 * (23.1a fix: build-variant/{tag} builds a NEW hardware tag from the existing
 * image and 409s on an existing variant — it never re-ran parity on the saved
 * edits, so it was the wrong runner for re-verify).
 *
 * Only global `fetch` is mocked — the page reads catalog + source with it, and
 * the components' authenticatedFetch ends up in the same global fetch.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import StoreDetailPage from "@/app/store/[kind]/[id]/page";

const res = (status: number, body: unknown) =>
  ({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown) as Response;

const CATALOG = {
  kind: "node",
  id: 42,
  name: "Acme Node",
  description: "A test listing",
  price_cents: 0,
  price_tier: null,
  owned: false,
  owned_via: null,
  versions: null,
  closure: null,
  actions: ["get"],
};

const SOURCE_DOC = {
  node_id: 42,
  name: "Acme Node",
  is_system: false,
  editable: true,
  current_revision_no: 3,
  revisions: [],
  hero: { dockerfile: "FROM base", files: [{ filename: "app.py", content: "import torch\n" }], origin: "snapshot" },
  variants: {
    rocm: { dockerfile: "FROM rocm", files: [{ filename: "app.py", content: "import torch\n" }], origin: "edited" },
  },
};

const VERDICTS = {
  node_definition_id: 42,
  reference_variant: "hero",
  variants: [
    {
      variant: "rocm",
      score: 0.4,
      verdict: "drift",
      scored_against_variant: "hero",
      output_type_at_test: "image",
      scored_at: "2026-10-03T09:30:00Z",
      attempt_count: 1,
      best_score: 0.4,
      drift_ruling: null,
      awaiting_drift_ruling: true,
    },
  ],
  pending_variants: [],
};

const route = (calls: Record<string, number>) =>
  (async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/catalog/")) return res(200, CATALOG);
    if (url.includes("/nodes/42/source")) return res(200, SOURCE_DOC);
    if (url.includes("/variant-verification")) return res(200, VERDICTS);
    if (url.includes("/nodes/42/rebuild")) {
      calls.builds = (calls.builds ?? 0) + 1;
      return res(200, { workflow_run_id: 777, workflow_id: 55, scope: "single_variant", critic_warnings: [] });
    }
    throw new Error(`unexpected url ${url}`);
  }) as unknown as typeof fetch;

async function openNodeDetail() {
  await act(async () => {
    render(<StoreDetailPage params={{ kind: "node", id: "42" }} />);
  });
  await screen.findByTestId("store-detail-name");
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  sessionStorage.setItem("workflow_access_token", "tok");
});

afterEach(() => {
  jest.restoreAllMocks();
});

describe("node detail — Variants panel", () => {
  it("renders the Variants panel with the verdict row for a node listing", async () => {
    global.fetch = route({});
    await openNodeDetail();

    expect(screen.getByTestId("store-variants-panel")).toBeInTheDocument();
    expect(screen.getByText(/Variant verdicts \(1\)/)).toBeInTheDocument();
    expect(screen.getByText("rocm")).toBeInTheDocument();
    expect(screen.getByText(/score 0\.40 · scored against hero · output type image/)).toBeInTheDocument();
    expect(screen.getByText(/diverged — waiting on you to rule/)).toBeInTheDocument();
  });

  it("renders no Variants panel for a non-node listing", async () => {
    global.fetch = route({});
    await act(async () => {
      render(<StoreDetailPage params={{ kind: "workflow", id: "7" }} />);
    });
    expect(screen.getByTestId("store-detail-name")).toBeInTheDocument();
    expect(screen.queryByTestId("store-variants-panel")).not.toBeInTheDocument();
  });

  it("a row's Edit code opens the editor modal for that variant", async () => {
    global.fetch = route({});
    await openNodeDetail();

    fireEvent.click(screen.getByTestId("variant-edit-code-rocm"));

    await waitForEditor();
    expect(screen.getByText(/^rocm · app\.py$/)).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toBeInTheDocument();
  });

  it("saving the variant re-reads the verdicts and offers re-verify", async () => {
    const calls: Record<string, number> = {};
    global.fetch = route(calls);
    await openNodeDetail();

    fireEvent.click(screen.getByTestId("variant-edit-code-rocm"));
    await waitForEditor();

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "import torch # patched" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save/i }));
    });

    await screen.findByTestId("store-variants-saved");
    expect(screen.getByTestId("store-variants-saved").textContent).toMatch(/rocm code saved/);
    expect(screen.getByText(/re-verify it to get a fresh verdict/)).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("variant-reverify-rocm"));
    await screen.findByTestId("variant-action-note-rocm");
    expect(calls.builds).toBe(1);
    expect(screen.getByTestId("variant-action-note-rocm").textContent)
      .toMatch(/re-verifying rocm with your saved code/);
  });

  it("shows the verdict list's own read error honestly inside the panel", async () => {
    global.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/catalog/")) return res(200, CATALOG);
      if (url.includes("/variant-verification")) return res(500, { detail: "upstream timeout" });
      if (url.includes("/nodes/42/source")) return res(200, SOURCE_DOC);
      throw new Error(`unexpected url ${url}`);
    }) as unknown as typeof fetch;
    await openNodeDetail();

    expect(screen.getByTestId("store-variants-panel")).toBeInTheDocument();
    expect(screen.getByText(/Verdicts unavailable/)).toBeInTheDocument();
    expect(screen.queryByText(/Variant verdicts \(\d+\)/)).not.toBeInTheDocument();
  });
});

async function waitForEditor() {
  await act(async () => {});
  await screen.findByText(/^rocm · app\.py$/);
}
