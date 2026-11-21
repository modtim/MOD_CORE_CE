/**
 * expr 3.6 — extraction-suggestion banner contract (read-only, no extract
 *  action).
 *
 * Proves:
 *  - items present → one compact info bar carrying the group/other-workflow
 *    counts ("N repeated node groups also appear in M other workflows —
 *    consider extracting a reusable sub-workflow");
 *  - the "Details" toggle lists per-item node_count + other-workflow names as
 *    plain text, capped at 10 rows;
 *  - dismiss (x) hides the bar and is remembered in sessionStorage per
 *    workflow id (a re-render fetches nothing and shows nothing);
 *  - empty items / 404 / network error → nothing renders, editor still
 *    renders (silent fallback);
 *  - workflowId null → no fetch at all.
 */

import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ExtractionSuggestionsBanner } from "@/components/extraction-suggestions-banner";

export function makeResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

export function suggestionsResponse(items: unknown[]): unknown {
  return { items };
}

const SUGGESTION = {
  canonical_hash: "abc123",
  hash_kind: "sha256",
  node_ids: ["1", "2", "3"],
  node_count: 3,
  other_workflow_count: 2,
  other_workflows: [{ id: 11, name: "Render Pack" }, { id: 12, name: "Upscale Pack" }],
};

const SECOND = {
  canonical_hash: "def456",
  hash_kind: "sha256",
  node_ids: ["4", "5"],
  node_count: 2,
  other_workflow_count: 1,
  other_workflows: [{ id: 13, name: "Audio Prep" }],
};

/** Render the banner exactly as workflow-builder.tsx does, with a stand-in
 *  editor after it. */
function renderBanner(
  fetchImpl: (url: string, method: string) => Promise<unknown> | unknown,
  workflowId: number | null,
) {
  global.fetch = jest.fn(
    (url: unknown, options?: unknown) => fetchImpl(String(url), String((options as { method?: string })?.method ?? "GET")),
  ) as unknown as typeof fetch;
  return render(
    <div data-testid="wb-view">
      <ExtractionSuggestionsBanner workflowId={workflowId} apiUrl="http://api.test" accessToken="tok" />
      <div data-testid="wb-editor">canvas</div>
    </div>,
  );
}

describe("ExtractionSuggestionsBanner — counts line", () => {
  it("renders the summary with N groups and M other workflows", async () => {
    renderBanner(async () => makeResponse(200, suggestionsResponse([SUGGESTION, SECOND])), 7);
    const note = await screen.findByRole("note");
    expect(note).toHaveTextContent(
      "2 repeated node groups also appear in 3 other workflows — consider extracting a reusable sub-workflow",
    );
    expect(note).toHaveAttribute("data-extraction-suggestions-banner", "1");
  });

  it("a single group counts as 1", async () => {
    renderBanner(async () => makeResponse(200, suggestionsResponse([SUGGESTION])), 7);
    const note = await screen.findByRole("note");
    expect(note).toHaveTextContent(
      "1 repeated node groups also appear in 2 other workflows — consider extracting a reusable sub-workflow",
    );
  });
});

describe("ExtractionSuggestionsBanner — Details toggle", () => {
  it("Details lists node_count + other workflow names as plain text", async () => {
    renderBanner(async () => makeResponse(200, suggestionsResponse([SUGGESTION, SECOND])), 7);
    const note = await screen.findByRole("note");
    const toggle = screen.getByTestId("extraction-details-toggle");
    fireEvent.click(toggle);
    const rows = note.querySelectorAll("[data-extraction-detail-row]");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("3 nodes — Render Pack, Upscale Pack");
    expect(rows[1]).toHaveTextContent("2 nodes — Audio Prep");
  });

  it("caps the detail list at 10 rows", async () => {
    const many = Array.from({ length: 14 }, (unused, index) => ({
      ...SUGGESTION,
      canonical_hash: `h${index}`,
      other_workflows: [{ id: 100 + index, name: `WF ${index}` }],
    }));
    renderBanner(async () => makeResponse(200, suggestionsResponse(many)), 7);
    const note = await screen.findByRole("note");
    fireEvent.click(screen.getByTestId("extraction-details-toggle"));
    expect(note.querySelectorAll("[data-extraction-detail-row]")).toHaveLength(10);
  });
});

describe("ExtractionSuggestionsBanner — dismiss (sessionStorage)", () => {
  it("dismiss hides the bar and persists per workflow id", async () => {
    renderBanner(async () => makeResponse(200, suggestionsResponse([SUGGESTION])), 7);
    const note = await screen.findByRole("note");
    fireEvent.click(screen.getByTestId("extraction-dismiss"));
    await act(async () => {});
    expect(screen.queryByRole("note")).toBeNull();
    expect(window.sessionStorage.getItem("EXTRACTION_SUGGESTIONS_DISMISSED_7")).toBe("1");
  });

  it("a fresh render for the dismissed workflow fetches nothing and shows nothing", async () => {
    window.sessionStorage.setItem("EXTRACTION_SUGGESTIONS_DISMISSED_9", "1");
    renderBanner(async () => makeResponse(200, suggestionsResponse([SUGGESTION])), 9);
    await act(async () => {});
    expect(global.fetch).not.toHaveBeenCalled();
    expect(screen.queryByRole("note")).toBeNull();
    expect(screen.getByTestId("wb-editor")).toHaveTextContent("canvas");
  });
});

describe("ExtractionSuggestionsBanner — fallbacks", () => {
  it("renders nothing when there are no suggestions", async () => {
    renderBanner(async () => makeResponse(200, suggestionsResponse([])), 8);
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(1));
    await act(async () => {});
    expect(screen.queryByRole("note")).toBeNull();
    expect(screen.getByTestId("wb-editor")).toHaveTextContent("canvas");
  });

  it("renders nothing on a 404 (endpoint unmounted / CE carve)", async () => {
    renderBanner(async () => makeResponse(404, {}), 8);
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(1));
    await act(async () => {});
    expect(screen.queryByRole("note")).toBeNull();
    expect(screen.getByTestId("wb-editor")).toHaveTextContent("canvas");
  });

  it("renders nothing on a network error and the editor still renders", async () => {
    renderBanner(async () => {
      throw new Error("network down");
    }, 8);
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(1));
    await act(async () => {});
    expect(screen.queryByRole("note")).toBeNull();
    expect(screen.getByTestId("wb-editor")).toHaveTextContent("canvas");
  });

  it("skips the fetch entirely when no workflow is open", async () => {
    renderBanner(async () => makeResponse(200, suggestionsResponse([SUGGESTION])), null);
    await act(async () => {});
    expect(global.fetch).not.toHaveBeenCalled();
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("requests the extraction-suggestions endpoint for the open workflow", async () => {
    renderBanner(async () => makeResponse(200, suggestionsResponse([SUGGESTION])), 42);
    await screen.findByRole("note");
    expect(global.fetch).toHaveBeenCalledWith(
      "http://api.test/road/42/extraction-suggestions",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer tok" }),
      }),
    );
  });
});


describe("ExtractionSuggestionsBanner — Extract as new workflow (expr 3.6c)", () => {
  const HASHY = {
    ...SUGGESTION,
    canonical_hash: "ab/c 12",
  };

  const extractUrl = "http://api.test/road/42/extraction-suggestions/ab%2Fc%2012/extract";

  it("button POSTs to the exact URL with the encoded hash", async () => {
    renderBanner(async () => makeResponse(200, suggestionsResponse([HASHY])), 42);
    const note = await screen.findByRole("note");
    fireEvent.click((note.querySelector("[data-testid='extraction-extract']") as HTMLElement));
    await waitFor(() =>
      expect(global.fetch).toHaveBeenCalledWith(
        extractUrl,
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("201 shows Created “{name}” with a deep link to the new workflow", async () => {
    renderBanner(
      async (_url: string, method: string) =>
        method === "POST"
          ? makeResponse(201, { workflow_id: 77, name: "Sub Pack", node_count: 3 })
          : makeResponse(200, suggestionsResponse([HASHY])),
      42,
    );
    const note = await screen.findByRole("note");
    fireEvent.click((note.querySelector("[data-testid='extraction-extract']") as HTMLElement));
    await waitFor(() => expect(note.querySelectorAll("[data-extraction-extract-status='created']")).toHaveLength(1));
    const created = (note.querySelector("[data-extraction-extract-status='created']") as HTMLElement);
    expect(created).toHaveTextContent("Created “Sub Pack”");
    expect(created.querySelector("[data-extraction-open-link]")).toHaveAttribute("href", "/?workflow=77");
  });

  it("button is disabled and shows Extracting… while the request is in flight", async () => {
    renderBanner(
      async (_url: string, method: string) =>
        method === "POST" ? new Promise<unknown>(() => {}) : makeResponse(200, suggestionsResponse([HASHY])),
      42,
    );
    const note = await screen.findByRole("note");
    fireEvent.click((note.querySelector("[data-testid='extraction-extract']") as HTMLElement));
    expect((note.querySelector("[data-testid='extraction-extract']") as HTMLElement)).toBeDisabled();
    const pending = note.querySelector("[data-extraction-extract-status='pending']");
    expect(pending).toBeTruthy();
    expect(pending).toHaveTextContent("Extracting…");
  });

  it("404 shows Extract failed inline", async () => {
    renderBanner(
      async (_url: string, method: string) =>
        method === "POST" ? makeResponse(404, { detail: "not_found" }) : makeResponse(200, suggestionsResponse([HASHY])),
      42,
    );
    const note = await screen.findByRole("note");
    fireEvent.click((note.querySelector("[data-testid='extraction-extract']") as HTMLElement));
    await waitFor(() => expect(note.querySelectorAll("[data-extraction-extract-status='failed']")).toHaveLength(1));
    expect(note.querySelector("[data-extraction-extract-status='failed']")).toHaveTextContent("Extract failed");
  });

  it("500 shows Extract failed inline", async () => {
    renderBanner(
      async (_url: string, method: string) =>
        method === "POST" ? makeResponse(500, {}) : makeResponse(200, suggestionsResponse([HASHY])),
      42,
    );
    const note = await screen.findByRole("note");
    fireEvent.click((note.querySelector("[data-testid='extraction-extract']") as HTMLElement));
    await waitFor(() => expect(note.querySelectorAll("[data-extraction-extract-status='failed']")).toHaveLength(1));
    expect(note.querySelector("[data-extraction-extract-status='failed']")).toHaveTextContent("Extract failed");
  });

  it("double-click sends one request and stays disabled after", async () => {
    renderBanner(
      async (_url: string, method: string) =>
        method === "POST"
          ? makeResponse(201, { workflow_id: 77, name: "Sub Pack", node_count: 3 })
          : makeResponse(200, suggestionsResponse([HASHY])),
      42,
    );
    const note = await screen.findByRole("note");
    const button = (note.querySelector("[data-testid='extraction-extract']") as HTMLElement);
    fireEvent.click(button);
    fireEvent.click(button);
    await act(async () => {});
    const posts = (global.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls.filter(
      (call) => call.length > 1 && (call[1] as { method?: string }).method === "POST",
    );
    expect(posts).toHaveLength(1);
    expect((note.querySelector("[data-testid='extraction-extract']") as HTMLElement)).toBeDisabled();
  });
});
