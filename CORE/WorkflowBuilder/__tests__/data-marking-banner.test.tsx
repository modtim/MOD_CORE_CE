/**
 * FX.79 p3a — data-marking banner contract (CUI/ITAR markings, Q62=A).
 *
 * Proves:
 *  - a marked workflow renders the canonical banner text
 *    (`CONTROLLED: <LABEL>[/<DISSEMINATION>] — handle per policy`, the
 *    same words CORE/API/services/marking_stamps.py stamps on exports);
 *  - the banner is persistent: rendered at BOTH the top and the bottom of
 *    the view, with role="note" + aria-label and no dismiss control;
 *  - worst label wins when a resource carries several marks, and the
 *    label ranking MATCHES marking_stamps.py (LABEL_RANK table +
 *    (-rank, label-code) fold, custom labels rank below the system set);
 *  - no markings → nothing renders;
 *  - fetch failure / non-OK response → no banner and the editor still
 *    renders (silent fallback, the editor is never blocked);
 *  - contrast: the banner keeps its ≥4.5:1 red-on-red-900 treatment.
 */

import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { DataMarkingBanner } from "@/components/data-marking-banner";
import { LABEL_RANK, CUSTOM_RANK, worstMarking, markingBannerText } from "@/lib/data-markings";
import { useEntitlements } from "@/hooks/use-entitlements";
jest.mock("@/hooks/use-entitlements", () => ({ useEntitlements: jest.fn() }));

const PAID_ENTITLEMENTS = { edition: null, loading: false, has: jest.fn(() => true), limit: jest.fn(() => null), tierName: null, resolved: true, declared: jest.fn(() => null) };
beforeEach(() => { (useEntitlements as jest.Mock).mockReturnValue(PAID_ENTITLEMENTS); });

// makeResponse / markingsResponse live in a non-test helper so other suites
// (fx163-marking-clear-visible) can share them without importing this test file.
import { makeResponse, markingsResponse } from "./helpers/data-marking-fixtures";

/** Render the persistent top+bottom pair exactly as workflow-builder.tsx
 *  does, with a stand-in editor between them. */
function renderBannerPair(fetchImpl: () => Promise<unknown> | never, workflowId: string | number | null) {
  global.fetch = jest.fn((url: unknown) => fetchImpl()) as unknown as typeof fetch;
  const makeMarkup = () => (
    <div data-testid="wb-view">
      <DataMarkingBanner resourceType="workflow" resourceId={workflowId} position="top" apiUrl="http://api.test" accessToken="tok" />
      <div data-testid="wb-editor">canvas</div>
      <DataMarkingBanner resourceType="workflow" resourceId={workflowId} position="bottom" apiUrl="http://api.test" accessToken="tok" />
    </div>
  );
  let utils: ReturnType<typeof render> | null = null;
  act(() => {
    utils = render(makeMarkup());
  });
  return { ...utils!, rerenderPair: () => act(() => utils!.rerender(makeMarkup())) };
}

describe("DataMarkingBanner — canonical wording (mirrors marking_stamps.py)", () => {
  it("renders CONTROLLED: LABEL — handle per policy for a single CUI mark", async () => {
    renderBannerPair(
      async () => makeResponse(200, markingsResponse([{ label: "CUI", dissemination: null }])),
      7,
    );
    await screen.findAllByRole("note");
    const top = screen.getByTestId("wb-view");
    expect(top.textContent).toBe(
      "CONTROLLED: CUI — handle per policycanvasCONTROLLED: CUI — handle per policy",
    );
    const banners = screen.getAllByRole("note");
    expect(banners).toHaveLength(2);
    for (const b of banners) {
      expect(b).toHaveTextContent("CONTROLLED: CUI — handle per policy");
    }
  });

  it("joins the dissemination addendum as LABEL/DISSEMINATION", async () => {
    renderBannerPair(
      async () => makeResponse(200, markingsResponse([{ label: "CUI", dissemination: "NOFORN" }])),
      7,
    );
    await screen.findAllByRole("note");
    for (const b of screen.getAllByRole("note")) {
      expect(b).toHaveTextContent("CONTROLLED: CUI/NOFORN — handle per policy");
    }
  });

  it("worst label wins when the resource carries several marks", async () => {
    // ITAR (90) beats CUI (70) and a custom label (40) — same fold as
    // marking_stamps.export_stamp.
    renderBannerPair(
      async () =>
        makeResponse(200, markingsResponse([
          { label: "CUI", dissemination: null },
          { label: "ACME-EXPORT", dissemination: null },
          { label: "ITAR", dissemination: "FED ONLY" },
        ])),
      7,
    );
    await screen.findAllByRole("note");
    for (const b of screen.getAllByRole("note")) {
      expect(b).toHaveTextContent("CONTROLLED: ITAR/FED ONLY — handle per policy");
    }
  });
});

describe("DataMarkingBanner — a11y + persistence", () => {
  it("carries role=note and an aria-label equal to the banner text", async () => {
    renderBannerPair(
      async () => makeResponse(200, markingsResponse([{ label: "ITAR" }])),
      7,
    );
    await screen.findAllByRole("note");
    const top = screen.getByTestId("wb-view");
    const banners = top.querySelectorAll("[role='note']");
    expect(banners).toHaveLength(2);
    banners.forEach((el) => {
      expect(el).toHaveAttribute("aria-label", "CONTROLLED: ITAR — handle per policy");
      // contrast ≥ 4.5:1 red family treatment, persistent (no close button)
      expect(el.className).toContain("bg-red-900");
      expect(el.querySelector("button")).toBeNull();
    });
    // one at the top, one at the bottom of the view
    expect(screen.getByTestId("wb-view").querySelector("[data-marking-banner='top']")).not.toBeNull();
    expect(screen.getByTestId("wb-view").querySelector("[data-marking-banner='bottom']")).not.toBeNull();
  });
});

describe("DataMarkingBanner — fallbacks", () => {
  it("renders nothing when the resource has no live markings", async () => {
    renderBannerPair(async () => makeResponse(200, { items: [], total: 0 }), 7);
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(2));
    await act(async () => {});
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("renders nothing on a fetch network error and the editor still renders", async () => {
    renderBannerPair(async () => {
      throw new Error("network down");
    }, 7);
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(2));
    await act(async () => {});
    expect(screen.queryByRole("note")).toBeNull();
    expect(screen.getByTestId("wb-editor")).toHaveTextContent("canvas");
  });

  it("renders nothing on non-OK responses (403 capability / 404 CE carve)", async () => {
    renderBannerPair(async () => makeResponse(403, {}), 7);
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(2));
    await act(async () => {});
    expect(screen.queryByRole("note")).toBeNull();
    expect(screen.getByTestId("wb-editor")).toHaveTextContent("canvas");
  });

  it("skips the fetch entirely when no workflow is open", async () => {
    renderBannerPair(async () => makeResponse(200, { items: [], total: 0 }), null);
    await act(async () => {});
    expect(global.fetch).not.toHaveBeenCalled();
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("requests the tenant data-markings endpoint for the open workflow", async () => {
    renderBannerPair(
      async () => makeResponse(200, markingsResponse([{ label: "CUI" }])),
      42,
    );
    await screen.findAllByRole("note");
    expect(global.fetch).toHaveBeenCalledWith(
      "http://api.test/tenant/data-markings/workflow/42",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer tok" }),
      }),
    );
  });
});

describe("DataMarkingBanner — FX.163 p3b4b Clear marking (tenant admin only)", () => {
  beforeEach(() => jest.restoreAllMocks());

  /** Render one banner whose fetch router answers GET + DELETE separately. */
  function renderClearable(
    handler: (url: string, init?: RequestInit) => Promise<unknown>,
    canClear: boolean,
  ) {
    global.fetch = jest.fn((url: unknown, init?: RequestInit) => handler(String(url), init)) as unknown as typeof fetch;
    return render(
      <DataMarkingBanner
        resourceType="workflow"
        resourceId={7}
        position="top"
        apiUrl="http://api.test"
        accessToken="tok"
        canClear={canClear}
      />,
    );
  }

  it("shows no Clear button by default (canClear defaults false)", async () => {
    renderClearable(async () => makeResponse(200, markingsResponse([{ label: "ITAR" }])), false);
    await screen.findByRole("note");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("renders one Clear button per label when canClear is true", async () => {
    renderClearable(
      async () => makeResponse(200, markingsResponse([{ label: "ITAR" }, { label: "CUI" }])),
      true,
    );
    const note = await screen.findByRole("note");
    const buttons = note.querySelectorAll("[data-marking-clear-label]");
    expect(buttons).toHaveLength(2);
    // worstMarking sorts the label set, so the buttons come out in label-code order
    expect(Array.from(buttons).map((b) => b.getAttribute("data-marking-clear-label"))).toEqual(["CUI", "ITAR"]);
    expect(Array.from(buttons).map((b) => b.textContent)).toEqual(["Clear", "Clear"]);
  });

  it("202 awaiting_approval → inline second-approver note and the label stays shown", async () => {
    const confirmSpy = jest.spyOn(window, "confirm").mockReturnValue(true);
    renderClearable(async (url) => {
      if (url.includes("/data-markings/workflow/7/ITAR")) {
        return makeResponse(202, { status: "awaiting_approval", approval_id: 4412 });
      }
      return makeResponse(200, markingsResponse([{ label: "ITAR" }]));
    }, true);
    const note = await screen.findByRole("note");
    await act(async () => {
      fireEvent.click(note.querySelector("[data-marking-clear-label='ITAR']")!);
    });
    expect(confirmSpy).toHaveBeenCalled();
    const shown = await screen.findByRole("note");
    expect(shown).toHaveTextContent("CONTROLLED: ITAR — handle per policy");
    // the 202 is the gate's answer, not an applied delete: no re-fetch GET happened
    const calls = (global.fetch as unknown as jest.Mock).mock.calls;
    expect(calls.filter((c) => (c[1]?.method ?? "GET") === "GET")).toHaveLength(1);
    expect(shown.querySelector("[data-marking-note]")).toHaveTextContent(
      "Clear data marking needs a second approver (request #4412)",
    );
    // the button survives so the admin can retry once the approver acts
    expect(shown.querySelector("[data-marking-clear-label='ITAR']")).not.toBeNull();
  });

  it("204 → re-fetches and the cleared label disappears", async () => {
    jest.spyOn(window, "confirm").mockReturnValue(true);
    let rows: Array<{ label: string }> = [{ label: "ITAR" }, { label: "CUI" }];
    renderClearable(async (url, init) => {
      if ((init?.method ?? "GET") === "DELETE") {
        rows = rows.filter((r) => r.label !== "ITAR");
        return makeResponse(204, null);
      }
      return makeResponse(200, markingsResponse(rows));
    }, true);
    const note = await screen.findByRole("note");
    await act(async () => {
      fireEvent.click(note.querySelector("[data-marking-clear-label='ITAR']")!);
    });
    await waitFor(() =>
      expect(global.fetch).toHaveBeenCalledWith(
        "http://api.test/tenant/data-markings/workflow/7/ITAR",
        expect.objectContaining({ method: "DELETE" }),
      ),
    );
    // GET → DELETE → GET (the re-fetch), no doubled /v1 in the path
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(3));
    const shown = await screen.findByRole("note");
    expect(shown).toHaveTextContent("CONTROLLED: CUI — handle per policy");
    expect(shown.querySelector("[data-marking-clear-label='ITAR']")).toBeNull();
    expect(shown.querySelector("[data-marking-note]")).toBeNull();
  });

  it("confirm cancelled → no DELETE at all", async () => {
    const confirmSpy = jest.spyOn(window, "confirm").mockReturnValue(false);
    renderClearable(async () => makeResponse(200, markingsResponse([{ label: "ITAR" }])), true);
    const note = await screen.findByRole("note");
    await act(async () => {
      fireEvent.click(note.querySelector("[data-marking-clear-label='ITAR']")!);
    });
    expect(confirmSpy).toHaveBeenCalled();
    await act(async () => {});
    const calls = (global.fetch as unknown as jest.Mock).mock.calls.map((c) => String(c[0]));
    expect(calls.some((u) => u.includes("/ITAR"))).toBe(false);
    expect(await screen.findByRole("note")).toHaveTextContent("CONTROLLED: ITAR — handle per policy");
  });

  it("server rejection → inline error text, marking still shown", async () => {
    jest.spyOn(window, "confirm").mockReturnValue(true);
    renderClearable(async (url, init) => {
      if ((init?.method ?? "GET") === "DELETE") return makeResponse(403, {});
      return makeResponse(200, markingsResponse([{ label: "ITAR" }]));
    }, true);
    const note = await screen.findByRole("note");
    await act(async () => {
      fireEvent.click(note.querySelector("[data-marking-clear-label='ITAR']")!);
    });
    const shown = await screen.findByRole("note");
    expect(shown.querySelector("[data-marking-note]")).toHaveTextContent(
      'Could not clear "ITAR" — the server rejected the request.',
    );
    expect(shown).toHaveTextContent("CONTROLLED: ITAR — handle per policy");
  });

  it("renders server-supplied text as text only (no HTML injection)", async () => {
    jest.spyOn(window, "confirm").mockReturnValue(true);
    renderClearable(async (url, init) => {
      if ((init?.method ?? "GET") === "DELETE") {
        return makeResponse(202, { status: "awaiting_approval", approval_id: "<img src=x onerror=alert(1)>" });
      }
      return makeResponse(200, markingsResponse([{ label: "ITAR" }]));
    }, true);
    const note = await screen.findByRole("note");
    await act(async () => {
      fireEvent.click(note.querySelector("[data-marking-clear-label='ITAR']")!);
    });
    const shown = await screen.findByRole("note");
    expect(shown.querySelector("[data-marking-note]")).toHaveTextContent(
      'Clear data marking needs a second approver (request #<img src=x onerror=alert(1)>)',
    );
    expect(shown.querySelector("img")).toBeNull();
  });
});

describe("worstMarking — ranking matches marking_stamps.py LABEL_RANK", () => {
  const PY_RANK = {
    ITAR: 90,
    "CUI//SP-EXPT": 80,
    CUI: 70,
    EXPORT_CONTROLLED: 60,
    EAR99: 55,
    PROPRIETARY: 50,
  };

  it("keeps the same rank table as marking_stamps.LABEL_RANK (customs at 40)", () => {
    expect(LABEL_RANK).toEqual(PY_RANK);
    expect(CUSTOM_RANK).toBe(40);
  });

  it.each([
    [["CUI", "ITAR"], "ITAR"],
    [["EAR99", "CUI//SP-EXPT"], "CUI//SP-EXPT"],
    [["PROPRIETARY", "EAR99"], "EAR99"],
    [["EXPORT_CONTROLLED", "PROPRIETARY"], "EXPORT_CONTROLLED"],
    [["PROPRIETARY", "ACME-CUSTOM"], "PROPRIETARY"], // custom (40) loses to system (50)
    [["ACME-B", "ACME-A"], "ACME-A"], // tie broken by label code, like the py sort
  ] as Array<[Array<string>, string]>)("wins with %j for marks %j", (marks, expected) => {
    const stamp = worstMarking(marks.map((m) => ({ label: m, dissemination: null })));
    expect(stamp?.label).toBe(expected);
  });

  it("ranks lowercase spellings by their system label (code case preserved)", () => {
    // case-insensitive rank lookup, like marking_stamps' code.upper() —
    // the winner keeps the stored code verbatim ("itar", not "ITAR").
    const stamp = worstMarking([{ label: "itar", dissemination: null }, { label: "CUI" }]);
    expect(stamp?.label).toBe("itar");
  });

  it("returns null for empty / blank-label rows", () => {
    expect(worstMarking([])).toBeNull();
    expect(worstMarking(null)).toBeNull();
    expect(worstMarking([{ label: "  " }, { label: null }])).toBeNull();
  });

  it("banner text keeps the em-dash convention verbatim", () => {
    const stamp = worstMarking([{ label: "CUI", dissemination: "NOFORN" }])!;
    expect(markingBannerText(stamp)).toBe("CONTROLLED: CUI/NOFORN — handle per policy");
  });
});

// ============================================================================
// 2400UI120 — CE edition gate: 6 cases asserting fetch calls to
// /tenant/data-markings/ (paid edition is fail-open: edition null → 1 call).
// ============================================================================
describe("2400UI120 CE edition gate", () => {
  const markingCalls = () =>
    (global.fetch as jest.Mock).mock.calls
      .map((c) => String(c[0]))
      .filter((u) => u.includes("/tenant/data-markings/"));

  beforeEach(() => {
    (useEntitlements as jest.Mock).mockReturnValue({
      edition: "ce",
      loading: false,
      has: jest.fn(() => true),
      limit: jest.fn(() => null),
      tierName: null,
      resolved: true,
      declared: jest.fn(() => null),
    });
  });
  afterEach(() => {
    jest.useRealTimers();
    jest.clearAllMocks();
  });

  it("(a) ce, not loading → 0 calls, and no banner", async () => {
    jest.useRealTimers();
    (useEntitlements as jest.Mock).mockReturnValue({
      edition: "ce",
      loading: false,
      has: jest.fn(() => true),
      limit: jest.fn(() => null),
      tierName: null,
      resolved: true,
      declared: jest.fn(() => null),
    });
    renderBannerPair(
      async () => makeResponse(200, markingsResponse([{ label: "CUI" }])),
      7,
    );
    await new Promise((r) => setTimeout(r, 50));
    expect(markingCalls()).toEqual([]);
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("(b) ce while loading → 0", async () => {
    (useEntitlements as jest.Mock).mockReturnValue({
      edition: "ce",
      loading: true,
      has: jest.fn(() => true),
      limit: jest.fn(() => null),
      tierName: null,
      resolved: false,
      declared: jest.fn(() => null),
    });
    renderBannerPair(
      async () => makeResponse(200, markingsResponse([{ label: "CUI" }])),
      7,
    );
    await new Promise((r) => setTimeout(r, 50));
    expect(markingCalls()).toEqual([]);
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("(c) null edition while loading, under fake timers → 0 before 5 s, then 1 per banner (2 total) after advancing 5 s", async () => {
    jest.useFakeTimers();
    (useEntitlements as jest.Mock).mockReturnValue({
      edition: null,
      loading: true,
      has: jest.fn(() => true),
      limit: jest.fn(() => null),
      tierName: null,
      resolved: false,
      declared: jest.fn(() => null),
    });
    renderBannerPair(
      async () => makeResponse(200, markingsResponse([{ label: "CUI" }])),
      7,
    );
    // 0 calls before the 5s gate opens
    expect(markingCalls()).toEqual([]);

    await act(async () => {
      jest.advanceTimersByTime(5000);
    });
    expect(markingCalls()).toHaveLength(2);
  });

  it("(d) saas, not loading → 1 per banner (2 total)", async () => {
    jest.useRealTimers();
    (useEntitlements as jest.Mock).mockReturnValue({
      edition: "saas",
      loading: false,
      has: jest.fn(() => true),
      limit: jest.fn(() => null),
      tierName: null,
      resolved: true,
      declared: jest.fn(() => null),
    });
    renderBannerPair(
      async () => makeResponse(200, markingsResponse([{ label: "CUI" }])),
      7,
    );
    await new Promise((r) => setTimeout(r, 50));
    expect(markingCalls()).toHaveLength(2);
  });

  it("(e) null, not loading → 1 per banner (2 total) (fail-open)", async () => {
    jest.useRealTimers();
    (useEntitlements as jest.Mock).mockReturnValue({
      edition: null,
      loading: false,
      has: jest.fn(() => true),
      limit: jest.fn(() => null),
      tierName: null,
      resolved: true,
      declared: jest.fn(() => null),
    });
    renderBannerPair(
      async () => makeResponse(200, markingsResponse([{ label: "CUI" }])),
      7,
    );
    await new Promise((r) => setTimeout(r, 50));
    expect(markingCalls()).toHaveLength(2);
  });

  it("(f) loading → resolved saas → 1 per banner (2 total), no double fetch", async () => {
    jest.useRealTimers();
    (useEntitlements as jest.Mock).mockReturnValue({
      edition: null,
      loading: true,
      has: jest.fn(() => true),
      limit: jest.fn(() => null),
      tierName: null,
      resolved: false,
      declared: jest.fn(() => null),
    });
    const pair = renderBannerPair(
      async () => makeResponse(200, markingsResponse([{ label: "CUI" }])),
      7,
    );
    await new Promise((r) => setTimeout(r, 50));
    expect(markingCalls()).toEqual([]);

    // Simulate: loading flipped false and edition resolved "saas" mid-render
    (useEntitlements as jest.Mock).mockReturnValue({
      edition: "saas",
      loading: false,
      has: jest.fn(() => true),
      limit: jest.fn(() => null),
      tierName: null,
      resolved: true,
      declared: jest.fn(() => null),
    });
    pair.rerenderPair();
    await new Promise((r) => setTimeout(r, 50));
    expect(markingCalls()).toHaveLength(2);

    pair.rerenderPair();
    await new Promise((r) => setTimeout(r, 50));
    expect(markingCalls()).toHaveLength(2);
  });
});
