/**
 * 23.1a.s1 — variant verdict list.
 *
 * The rows below mirror GET /v1/nodes/{id}/variant-verification exactly as
 * CORE/API/services/node_authoring_service.py:641 builds it:
 *   { node_definition_id, reference_variant, variants: [_VariantVerificationRow],
 *     pending_variants: [str] }
 * with per-row { variant, score, verdict, scored_against_variant,
 * output_type_at_test, scored_at, attempt_count, best_score, drift_ruling,
 * awaiting_drift_ruling }.
 *
 * The assertions that matter are the honest ones: a verdict row must carry
 * its verdict + reason + timestamp, and a failed read must NOT render as an
 * empty list (an empty list reads as "nothing verified" and invites someone
 * to ship an unchecked variant).
 */
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import VariantVerdictList from "@/components/variant-verdict-list";
import { authenticatedFetch } from "@/lib/api-client";

jest.mock("@/lib/api-client", () => ({
  __esModule: true,
  authenticatedFetch: jest.fn(),
}));

const mockFetch = authenticatedFetch as jest.MockedFunction<typeof authenticatedFetch>;

const jsonOk = (body: unknown) =>
  ({ ok: true, status: 200, json: async () => body }) as unknown as Response;

const API = "http://api.test";

const jsonFail = (status: number, detail: unknown) =>
  ({
    ok: false,
    status,
    json: async () => ({ detail }),
    text: async () => JSON.stringify({ detail }),
  } as unknown) as Response;

const SOURCE_DOC = {
  node_id: 42,
  name: "My Node",
  is_system: false,
  editable: true,
  current_revision_no: 3,
  revisions: [],
  hero: { dockerfile: "FROM x", files: [{ filename: "app.py", content: "import torch\n" }], origin: "snapshot" },
  variants: {
    rocm: {
      dockerfile: "FROM rocm-base",
      files: [{ filename: "app.py", content: "import torch\n" }],
      origin: "edited",
    },
  },
};

const PATCH_OK = {
  dispatched: true,
  task_id: 909,
  attempts_used: 1,
  attempts_remaining: 2,
  token_total: 20000,
  token_remaining: 18000,
  diff_hash: "d1",
  confidence: 0.62,
  source: "llm",
  target_files: ["app.py"],
};

const PATCH_REFUSED = {
  dispatched: false,
  reason_code: "cap",
  refuse_reason: "attempt cap reached for this stage",
  attempts_used: 3,
  attempts_remaining: 0,
  token_total: 20000,
  token_remaining: 4000,
};

const REVERIFY_OK = { workflow_run_id: 777, workflow_id: 55, scope: "single_variant", critic_warnings: [] };

function verificationBody(rows: unknown[], pending: string[] = []) {
  return {
    node_definition_id: 42,
    reference_variant: "hero",
    variants: rows,
    pending_variants: pending,
  };
}

const CUDA_ROW = {
  variant: "cuda",
  score: 0.91,
  verdict: "verified",
  scored_against_variant: "hero",
  output_type_at_test: "image",
  scored_at: "2026-10-01T12:00:00Z",
  attempt_count: 2,
  best_score: 0.95,
  image_digest: "sha256:abc",
  attempt_history: [],
  drift_ruling: null,
  awaiting_drift_ruling: false,
};

const ROCM_ROW = {
  variant: "rocm",
  score: 0.4,
  verdict: "drift",
  scored_against_variant: "hero",
  output_type_at_test: "image",
  scored_at: "2026-10-03T09:30:00Z",
  attempt_count: 1,
  best_score: 0.4,
  image_digest: null,
  attempt_history: [],
  drift_ruling: null,
  awaiting_drift_ruling: true,
};

beforeEach(() => {
  mockFetch.mockReset();
});

describe("VariantVerdictList", () => {
  it("renders one row per variant with verdict, reason and timestamp", async () => {
    mockFetch.mockImplementation((url: string) => {
      if (String(url).includes("/nodes/42/variant-verification")) {
        return Promise.resolve(jsonOk(verificationBody([CUDA_ROW, ROCM_ROW])));
      }
      throw new Error(`unexpected url ${url}`);
    });

    render(<VariantVerdictList nodeId={42} apiUrl={API} />);

    await waitFor(() => {
      expect(screen.getByText(/score 0\.91 · best 0\.95 · scored against hero · output type image · 2 attempts/))
        .toBeInTheDocument();
    }, { timeout: 20000 });

    expect(screen.getByText("cuda")).toBeInTheDocument();
    expect(screen.getByText("rocm")).toBeInTheDocument();
    expect(screen.getByText("verified")).toBeInTheDocument();
    expect(screen.getByText(/diverged — waiting on you to rule/)).toBeInTheDocument();
    // Timestamps render, so "never scored" must not appear for scored rows.
    expect(screen.queryByText(/never scored/)).toBeNull();
  });

  it("marks a drift row awaiting a ruling distinctly from a ruled one", async () => {
    const ruled = { ...ROCM_ROW, drift_ruling: "publish", awaiting_drift_ruling: false };
    mockFetch.mockImplementation((url: string) =>
      Promise.resolve(jsonOk(verificationBody([ROCM_ROW, ruled])))
    );

    render(<VariantVerdictList nodeId={42} apiUrl={API} />);

    await waitFor(() => {
      expect(screen.getByText(/drift ruled publish/)).toBeInTheDocument();
    }, { timeout: 20000 });
    expect(screen.getByText(/diverged — waiting on you to rule/)).toBeInTheDocument();
  });

  it("shows pending variants as pending, never as a verdict", async () => {
    mockFetch.mockImplementation((url: string) =>
      Promise.resolve(jsonOk(verificationBody([CUDA_ROW], ["cpu", "npu"])))
    );

    render(<VariantVerdictList nodeId={42} apiUrl={API} />);

    await waitFor(() => {
      expect(screen.getAllByText("pending").length).toBe(2);
    }, { timeout: 20000 });
    expect(screen.getByText(/verifying cpu, npu/)).toBeInTheDocument();
    expect(screen.getAllByText("no verdict yet").length).toBe(2);
  });

  it("renders an explicit empty state when nothing is verified", async () => {
    mockFetch.mockImplementation((url: string) =>
      Promise.resolve(jsonOk(verificationBody([])))
    );

    render(<VariantVerdictList nodeId={42} apiUrl={API} />);

    await waitFor(() => {
      expect(screen.getByText(/No variants verified yet/)).toBeInTheDocument();
    }, { timeout: 20000 });
  });

  it("surfaces a non-ok read as an error, not as an empty list", async () => {
    mockFetch.mockImplementation((url: string) =>
      Promise.resolve(({ ok: false, status: 500, json: async () => ({}) } as unknown) as Response)
    );

    render(<VariantVerdictList nodeId={42} apiUrl={API} />);

    await waitFor(() => {
      expect(screen.getByText(/Verdicts unavailable: 500/)).toBeInTheDocument();
    }, { timeout: 20000 });
    expect(screen.queryByText(/No variants verified yet/)).toBeNull();
  });

  it("fails closed when the read throws", async () => {
    mockFetch.mockRejectedValue(new Error("network down"));

    render(<VariantVerdictList nodeId={42} apiUrl={API} />);

    await waitFor(() => {
      expect(screen.getByText(/Verdicts unavailable/)).toBeInTheDocument();
    }, { timeout: 20000 });
    expect(screen.queryByText(/No variants verified yet/)).toBeNull();
  });

  it("calls onLoaded with the parsed rows after a successful read", async () => {
    const seen: string[] = [];
    mockFetch.mockImplementation((url: string) =>
      Promise.resolve(jsonOk(verificationBody([CUDA_ROW, ROCM_ROW])))
    );

    render(
      <VariantVerdictList
        nodeId={42}
        apiUrl={API}
        onLoaded={(rows) => rows.forEach((r) => seen.push(r.variant))}
      />
    );

    await waitFor(() => {
      expect(seen).toEqual(["cuda", "rocm"]);
    }, { timeout: 20000 });
  });

  // ---------------------------------------------------------------------
  // 23.1a.s3 — per-row actions. Routes + bodies are pinned in
  // lib/variant-actions.ts (POST /node-definitions/{id}/variants/{v}/patch
  // and POST /nodes/{id}/rebuild {scope:"single_variant"}); the assertions here are about
  // the UI contract: busy is per row, a refusal is shown as a note (not an
  // error), a failed action is shown on THAT row only, and after any dispatch
  // the row is refreshed by re-reading the verification document.
  // ---------------------------------------------------------------------

  const actionsMock = (patch: () => Promise<Response>, pending: string[] = []) => {
    mockFetch.mockImplementation((url: string) => {
      const target = String(url);
      if (target.includes("/variant-verification")) {
        return Promise.resolve(jsonOk(verificationBody([CUDA_ROW, ROCM_ROW], pending)));
      }
      if (target.includes("/nodes/42/source")) return Promise.resolve(jsonOk(SOURCE_DOC));
      if (target.includes("/variants/rocm/patch") || target.includes("/nodes/42/rebuild")) return patch();
      throw new Error(`unexpected url ${target}`);
    });
  };

  it("offers repair + re-verify buttons on every verdict row", async () => {
    actionsMock(() => Promise.resolve(jsonOk(PATCH_OK)), ["npu"]);

    render(<VariantVerdictList nodeId={42} apiUrl={API} />);

    await waitFor(() => {
      expect(screen.getByText("cuda")).toBeInTheDocument();
    }, { timeout: 20000 });

    expect(screen.queryByTestId("variant-repair-rocm")).toBeDefined();
    expect(screen.queryByTestId("variant-reverify-rocm")).toBeDefined();
    expect(screen.queryByTestId("variant-repair-cuda")).toBeDefined();
    // Pending variants get no buttons — there is no verdict to act on.
    expect(screen.getByText(/verifying npu/)).toBeInTheDocument();
    expect(screen.queryByTestId("variant-repair-npu")).toBeNull();
    expect(screen.queryByTestId("variant-reverify-npu")).toBeNull();
  });

  it("asks the wizard to repair a drifted variant and refreshes the row", async () => {
    actionsMock(() => Promise.resolve(jsonOk(PATCH_OK)));

    render(<VariantVerdictList nodeId={42} apiUrl={API} />);

    await waitFor(() => {
      expect(screen.getByText("cuda")).toBeInTheDocument();
    }, { timeout: 20000 });

    fireEvent.click(screen.getByTestId("variant-repair-rocm"));

    await waitFor(() => {
      expect(screen.getByTestId("variant-action-note-rocm")).toBeInTheDocument();
    }, { timeout: 20000 });
    expect(screen.getByTestId("variant-action-note-rocm").textContent).toMatch(/patch proposed/);
    // The row re-read itself: the verdict is still shown, not cleared.
    expect(screen.getByText("rocm")).toBeInTheDocument();
    expect(screen.queryByTestId("variant-action-error-rocm")).toBeNull();
  });

  it("shows a refused patch as a note, not as an error", async () => {
    actionsMock(() => Promise.resolve(jsonOk(PATCH_REFUSED)));

    render(<VariantVerdictList nodeId={42} apiUrl={API} />);

    await waitFor(() => {
      expect(screen.getByText("cuda")).toBeInTheDocument();
    }, { timeout: 20000 });

    fireEvent.click(screen.getByTestId("variant-repair-rocm"));

    await waitFor(() => {
      expect(screen.getByTestId("variant-action-note-rocm")).toBeInTheDocument();
    }, { timeout: 20000 });
    expect(screen.getByTestId("variant-action-note-rocm").textContent).toMatch(/attempt cap reached/);
    expect(screen.queryByTestId("variant-action-error-rocm")).toBeNull();
  });

  it("surfaces a failed re-verify on that row only", async () => {
    actionsMock(() =>
      Promise.resolve(jsonFail(409, { code: "registry_not_isolated" }))
    );

    render(<VariantVerdictList nodeId={42} apiUrl={API} />);

    await waitFor(() => {
      expect(screen.getByText("cuda")).toBeInTheDocument();
    }, { timeout: 20000 });

    fireEvent.click(screen.getByTestId("variant-reverify-rocm"));

    await waitFor(() => {
      expect(screen.getByTestId("variant-action-error-rocm")).toBeInTheDocument();
    }, { timeout: 20000 });
    expect(screen.getByTestId("variant-action-error-rocm").textContent)
      .toMatch(/no tenant registry to rebuild into/);
    // The other row is untouched — no error, no busy spinner.
    expect(screen.queryByTestId("variant-action-error-cuda")).toBeNull();
    expect(screen.queryByTestId("variant-action-busy-cuda")).toBeNull();
    // A failed action must not read as a lost verdict list.
    expect(screen.queryByText(/Verdicts unavailable/)).toBeNull();
  });

  it("shows busy state on the row being re-verified and hides its buttons", async () => {
    const pendingFetch = () => new Promise<Response>(() => { /* still in flight */ });
    actionsMock(pendingFetch);

    render(<VariantVerdictList nodeId={42} apiUrl={API} />);

    await waitFor(() => {
      expect(screen.getByText("cuda")).toBeInTheDocument();
    }, { timeout: 20000 });

    fireEvent.click(screen.getByTestId("variant-reverify-rocm"));

    await waitFor(() => {
      expect(screen.getByTestId("variant-action-busy-rocm")).toBeInTheDocument();
    }, { timeout: 20000 });
    expect(screen.getByText(/re-verifying…/)).toBeInTheDocument();
    // Buttons for the busy row are gone so a double click cannot double-dispatch.
    expect(screen.queryByTestId("variant-reverify-rocm")).toBeNull();
    // The other row still offers its actions.
    expect(screen.queryByTestId("variant-repair-cuda")).toBeDefined();
  });

  it("keeps the rows when the post-dispatch re-read fails", async () => {
    let verificationReads = 0;
    mockFetch.mockImplementation((url: string) => {
      const target = String(url);
      if (target.includes("/variant-verification")) {
        verificationReads += 1;
        if (verificationReads === 1) return Promise.resolve(jsonOk(verificationBody([CUDA_ROW, ROCM_ROW])));
        return Promise.resolve(jsonFail(500, "upstream timeout"));
      }
      if (target.includes("/nodes/42/source")) return Promise.resolve(jsonOk(SOURCE_DOC));
      if (target.includes("/nodes/42/rebuild")) return Promise.resolve(jsonOk(REVERIFY_OK));
      throw new Error(`unexpected url ${target}`);
    });

    render(<VariantVerdictList nodeId={42} apiUrl={API} />);

    await waitFor(() => {
      expect(screen.getByText("cuda")).toBeInTheDocument();
    }, { timeout: 20000 });

    fireEvent.click(screen.getByTestId("variant-reverify-rocm"));

    await waitFor(() => {
      expect(screen.getByTestId("variant-action-note-rocm")).toBeInTheDocument();
    }, { timeout: 20000 });
    expect(verificationReads).toBe(2);
    // The stale-but-real rows survive a failed refresh; the list is not blanked
    // and not marked unavailable.
    expect(screen.getByText("cuda")).toBeInTheDocument();
    expect(screen.queryByText(/Verdicts unavailable/)).toBeNull();
  });
});
