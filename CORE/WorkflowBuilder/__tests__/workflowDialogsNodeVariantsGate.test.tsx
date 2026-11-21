/**
 * 2400LR-b6g — CE carve on the Edit-Nodes dialog's hardware-variant fetch.
 *
 * WorkflowDialogs.tsx EditNodesContent fetches GET /accelerators/node-variants
 * on every dialog open. That route lives in CORE/API/routers/gpu_settings.py,
 * which open-core/manifest.yaml DENIES (:1289 the router, :1537 gpu_settings*.py)
 * — so on CE the call 404s and the e2e assertClean guard flags it.
 *
 * The gate is the SAME edition signal the 2400UI120 data-markings fix uses
 * (components/data-marking-banner.tsx:78-121): useEntitlements() over the
 * /platform/features `entitlements.edition` block, FAIL-OPEN — the fetch is
 * skipped ONLY on a positively-resolved edition "ce"; unresolved/errored leaves
 * edition null and paid behaviour (same call, same timing) is untouched.
 */

import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { _resetPlatformFeaturesCache } from "@/lib/platform-features";
import { WorkflowDialogs } from "@/components/WorkflowDialogs";

const NODES = [
  { id: 1, name: "Blur", type: "Process", version: 1, status: "active" as any },
];

const calls: () => string[] = () =>
  ((global.fetch as jest.Mock).mock.calls || []).map((a: any[]) => String(a[0]));

const featuresPayload = (edition: string | null) => ({
  ok: true,
  json: () =>
    Promise.resolve({
      deployment_mode: "saas",
      entitlements: { edition, capabilities: ["nodes"], limits: {} },
    }),
});

const openEditDialog = () => {
  const noop = () => {};
  const props = {
    isSaveDialogOpen: false, setSaveDialogOpen: noop, saveDialogMode: "new" as any,
    newWorkflowName: "", setNewWorkflowName: noop, handleSaveConfirm: noop,
    isLoadDialogOpen: false, setLoadDialogOpen: noop, dialogView: "workflows" as any,
    setDialogView: noop, workflows: [], versions: [], currentWorkflow: null,
    handleWorkflowSelection: noop, handleVersionSelection: noop,
    isEditDialogOpen: true, setEditDialogOpen: noop, isRefreshing: false,
    deletableNodes: NODES, handleDeleteInitiation: noop, handleRefreshAllNodes: noop,
    accessToken: "tok",
  } as any;
  render(<WorkflowDialogs {...props} />);
};

beforeEach(() => {
  _resetPlatformFeaturesCache();
  global.fetch = jest.fn() as any;
});

afterEach(() => {
  global.fetch = undefined as any;
});

describe("2400LR-b6g node-variants CE edition gate", () => {
  it("CE edition: dialog opens, /accelerators/node-variants is never fetched", async () => {
    (global.fetch as jest.Mock).implements = (url: string) =>
      url.includes("/platform/features")
        ? featuresPayload("ce")
        : { ok: true, json: () => Promise.resolve({}) };
    openEditDialog();
    // Flush until the entitlements block has resolved (the gate's precondition).
    await waitFor(() => expect(calls().some((u) => u.includes("/platform/features"))).toBe(true));
    expect(calls().filter((u) => u.includes("/accelerators/node-variants")).length).toBe(0);
    expect(screen.getByText("Edit Node Definitions")).toBeTruthy();
  });

  it("paid edition: /accelerators/node-variants IS fetched (behaviour unchanged)", async () => {
    (global.fetch as jest.Mock).implements = (url: string) =>
      url.includes("/platform/features")
        ? featuresPayload("pro")
        : { ok: true, json: () => Promise.resolve({ enabled_tags: ["cuda"], nodes: [] }) };
    openEditDialog();
    await waitFor(
      () => expect(calls().some((u) => u.includes("/accelerators/node-variants"))).toBe(true),
    );
  });

  it("unresolved entitlements stay on the PAID path (fail open)", async () => {
    (global.fetch as jest.Mock).implements = (url: string) =>
      url.includes("/platform/features")
        ? { ok: false, status: 500, json: () => Promise.resolve({}) }
        : { ok: true, json: () => Promise.resolve({}) };
    openEditDialog();
    await waitFor(() => expect(calls().some((u) => u.includes("/accelerators/node-variants"))).toBe(true));
  });
});
