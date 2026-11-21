/**
 * 23.1a.s2 — per-variant code editor modal.
 *
 * Shapes come straight from CORE/API/routers/node_code_edit.py:
 *   GET  /{node_id}/source → { node_id, name, is_system, editable,
 *                              current_revision_no, revisions, hero,
 *                              variants: {variant: {dockerfile, files, origin}} }
 *                              (node_code_edit.py:198, variants_view() at
 *                              services/node_code_edit_service.py:66)
 *   POST /{node_id}/source → body SourceEditRequest
 *                              { hero?, source_overrides_by_variant, message? }
 *                              (node_code_edit.py:107) → 201 { revision_no, origin }
 *
 * There is no `variant` query parameter on either route — the whole document
 * is read and the variant is selected client-side.
 *
 * The load-bearing assertion is the last one: POST replaces the whole
 * source_overrides_by_variant map (node_code_edit.py:243), so saving cuda
 * must re-send rocm's existing edits or they silently vanish.
 */
import React from "react";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import VariantCodeEditor from "@/components/variant-code-editor";
import { authenticatedFetch } from "@/lib/api-client";

jest.mock("@/lib/api-client", () => ({
  __esModule: true,
  authenticatedFetch: jest.fn(),
}));

const mockFetch = authenticatedFetch as jest.MockedFunction<typeof authenticatedFetch>;

const API = "http://api.test";

const SOURCE_DOC = {
  node_id: 42,
  name: "Upscaler",
  is_system: false,
  editable: true,
  current_revision_no: 3,
  revisions: [],
  hero: { dockerfile: "FROM hero" },
  variants: {
    cuda: {
      dockerfile: "FROM cuda-image",
      files: [{ filename: "run.sh", content: "cuda original" }],
      origin: "edited",
    },
    rocm: {
      dockerfile: "FROM rocm-image",
      files: [{ filename: "run.sh", content: "rocm original" }],
      origin: "edited",
    },
    cpu: {
      dockerfile: "FROM cpu-image",
      files: [],
      origin: "generated",
    },
  },
};

function okPost(body: unknown) {
  return ({ ok: true, status: 201, json: async () => body }) as unknown as Response;
}

beforeEach(() => {
  mockFetch.mockReset();
});

describe("VariantCodeEditor", () => {
  it("renders nothing while closed", async () => {
    mockFetch.mockImplementation((url: string) =>
      Promise.resolve(({ ok: true, status: 200, json: async () => SOURCE_DOC } as unknown) as Response)
    );

    const { container } = render(
      <VariantCodeEditor nodeId={42} apiUrl={API} variant="cuda" filename="run.sh" open={false} onClose={() => {}} />
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("shows the requested variant's file content", async () => {
    mockFetch.mockImplementation((url: string) =>
      Promise.resolve(({ ok: true, status: 200, json: async () => SOURCE_DOC } as unknown) as Response)
    );

    render(
      <VariantCodeEditor nodeId={42} apiUrl={API} variant="cuda" filename="run.sh" open onClose={() => {}} />
    );

    await waitFor(() => {
      expect(screen.getByText(/cuda · run\.sh/)).toBeInTheDocument();
    });
    const editor = screen.getByRole("textbox");
    expect(editor).toHaveTextContent("cuda original");
  });

  it("typing marks the editor unsaved", async () => {
    mockFetch.mockImplementation((url: string) =>
      Promise.resolve(({ ok: true, status: 200, json: async () => SOURCE_DOC } as unknown) as Response)
    );

    render(
      <VariantCodeEditor nodeId={42} apiUrl={API} variant="cuda" filename="run.sh" open onClose={() => {}} />
    );

    await waitFor(() => {
      expect(screen.getByRole("textbox")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "cuda patched" } });

    await waitFor(() => {
      expect(screen.getByText(/Unsaved changes/)).toBeInTheDocument();
    });
  });

  it("sends the edited variant plus the other variants' existing overrides", async () => {
    const posts: unknown[] = [];
    mockFetch.mockImplementation((_url: string, opts: unknown) => {
      if (opts && (opts as { method?: string }).method === "POST") {
        posts.push(JSON.parse(String((opts as { body?: string }).body)));
        return Promise.resolve(okPost({ revision_no: 4, origin: "edited" }));
      }
      return Promise.resolve(({ ok: true, status: 200, json: async () => SOURCE_DOC } as unknown) as Response);
    });

    render(
      <VariantCodeEditor nodeId={42} apiUrl={API} variant="cuda" filename="run.sh" open onClose={() => {}} />
    );

    await waitFor(() => {
      expect(screen.getByRole("textbox")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "cuda patched" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => {
      expect(screen.getByText(/^Saved\./)).toBeInTheDocument();
    });

    const sent = (posts[0] ?? {}) as { source_overrides_by_variant?: Record<string, unknown> };
    const overrides = sent.source_overrides_by_variant ?? {};
    expect(overrides.cuda).toEqual([{ filename: "run.sh", content: "cuda patched" }]);
    // rocm's untouched edit survives the replace-the-whole-map POST.
    expect(overrides.rocm).toEqual([{ filename: "run.sh", content: "rocm original" }]);
    // cpu was never edited, so it is not re-sent as an override.
    expect(overrides.cpu).toBeUndefined();
  });

  it("shows the error and keeps the text when the save fails", async () => {
    mockFetch.mockImplementation((url: string, opts: unknown) => {
      if (opts && (opts as { method?: string }).method === "POST") {
        return Promise.resolve(
          ({ ok: false, status: 409, text: async () => '{"detail":{"code":"must_fork"}}' } as unknown) as Response
        );
      }
      return Promise.resolve(({ ok: true, status: 200, json: async () => SOURCE_DOC } as unknown) as Response);
    });

    render(
      <VariantCodeEditor nodeId={42} apiUrl={API} variant="cuda" filename="run.sh" open onClose={() => {}} />
    );

    await waitFor(() => {
      expect(screen.getByRole("textbox")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "cuda patched" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => {
      expect(screen.getByText(/Not saved: This is a platform node/)).toBeInTheDocument();
    });
    expect(screen.getByRole("textbox")).toHaveTextContent("cuda patched");
    expect(screen.queryByText(/^Saved\./)).toBeNull();
  });

  it("offers no save button for a platform node or a dockerfile", async () => {
    const systemDoc = { ...SOURCE_DOC, editable: false, is_system: true };
    mockFetch.mockImplementation((url: string) =>
      Promise.resolve(({ ok: true, status: 200, json: async () => systemDoc } as unknown) as Response)
    );

    render(
      <VariantCodeEditor nodeId={42} apiUrl={API} variant="cuda" filename="dockerfile" open onClose={() => {}} />
    );

    await waitFor(() => {
      expect(screen.getByText(/cuda · dockerfile/)).toBeInTheDocument();
    });
    expect(screen.queryByRole("button", { name: /save/i })).toBeNull();
    expect(screen.getByText(/Dockerfiles are regenerated by the build/)).toBeInTheDocument();
  });

  it("surfaces a failed read as an error dialog, never as an empty editor", async () => {
    mockFetch.mockRejectedValue(new Error("network down"));

    render(
      <VariantCodeEditor nodeId={42} apiUrl={API} variant="cuda" filename="run.sh" open onClose={() => {}} />
    );

    await waitFor(() => {
      expect(screen.getByText(/Source unavailable/)).toBeInTheDocument();
    });
    expect(screen.getByText(/network down/)).toBeInTheDocument();
  });
});
