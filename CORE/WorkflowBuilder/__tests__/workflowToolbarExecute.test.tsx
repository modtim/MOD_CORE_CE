/**
 * Execute button must never be a silent dead click on an unsaved
 * workflow. Owner bug: "execute button, if the user hasn't saved,
 * should ask the user to save."
 *
 * Regression guard: the Execute button stays ENABLED even when no
 * workflow is loaded (never saved) so the click can route into the
 * save prompt, and it carries a title explaining that it will prompt
 * to save first. The actual save-then-run decision lives in
 * useWorkflowApi.executeWorkflow; here we only assert the button is
 * not a dead control and always calls its handler.
 */

import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import * as fs from "fs";
import * as path from "path";

// Stub the heavy child surfaces so the toolbar renders without network
// or auth. None are involved in the Execute-button behaviour under test.
jest.mock("@/components/TriggerCreateDialog", () => ({
  __esModule: true,
  TriggerCreateDialog: () => null,
}));
jest.mock("@/components/ShareAppDialog", () => ({
  __esModule: true,
  ShareAppDialog: () => null,
}));
jest.mock("@/components/WorkflowShareDialog", () => ({
  __esModule: true,
  WorkflowShareDialog: () => null,
}));
jest.mock("@/components/WorkflowCostDisplay", () => ({
  __esModule: true,
  WorkflowCostDisplay: () => null,
}));

import { WorkflowToolbar } from "@/components/WorkflowToolbar";

const baseProps = {
  onSave: jest.fn(),
  onSaveAs: jest.fn(),
  onLoad: jest.fn(),
  onEdit: jest.fn(),
  onExecute: jest.fn(),
  onQueue: jest.fn(),
  onExport: jest.fn(),
  onImportFile: jest.fn(),
  nodes: [],
  edges: [],
  accessToken: null as string | null,
};

describe("WorkflowToolbar Execute button (unsaved-run UX)", () => {
  it("stays enabled and calls onExecute when no workflow is loaded", () => {
    const onExecute = jest.fn();
    render(
      <WorkflowToolbar
        {...baseProps}
        onExecute={onExecute}
        isWorkflowLoaded={false}
        activeRunId={null}
      />
    );
    const btn = screen.getByRole("button", { name: /execute/i });
    expect(btn).not.toBeDisabled();
    fireEvent.click(btn);
    expect(onExecute).toHaveBeenCalledTimes(1);
  });

  it("explains via its title that clicking will prompt to save when unsaved", () => {
    render(
      <WorkflowToolbar
        {...baseProps}
        isWorkflowLoaded={false}
        activeRunId={null}
      />
    );
    const btn = screen.getByRole("button", { name: /execute/i });
    expect(btn).toHaveAttribute("title", expect.stringMatching(/save/i));
  });

  it("is enabled and executes normally when a workflow is loaded", () => {
    const onExecute = jest.fn();
    render(
      <WorkflowToolbar
        {...baseProps}
        onExecute={onExecute}
        isWorkflowLoaded={true}
        activeRunId={null}
      />
    );
    const btn = screen.getByRole("button", { name: /execute/i });
    expect(btn).not.toBeDisabled();
    fireEvent.click(btn);
    expect(onExecute).toHaveBeenCalledTimes(1);
  });

  it("mounts outside the ReactFlow container so its menus are not clipped", () => {
    // ReactFlow's wrapper div carries inline overflow:'hidden'
    // (@reactflow/core wrapperStyle), so anything mounted as a ReactFlow
    // child is clipped to the canvas box. The toolbar's Actions/Save
    // dropdowns hang LEFT of that box (sidebar width 420 + 4px separator)
    // and were unclickable there — the Load/Actions menu items sat under
    // the node-library sidebar. The toolbar must therefore be a sibling of
    // ReactFlow inside the canvas container, which is position:relative so
    // the absolute top-right anchor survives the move.
    const wb = fs.readFileSync(
      path.resolve(__dirname, "../components/workflow-builder.tsx"),
      "utf-8",
    );
    const rfClose = wb.indexOf("</ReactFlow>");
    const toolbarMount = wb.indexOf(
      "<div style={{ position: 'absolute', top: 10, right: 10, zIndex: 5 }}",
    );
    expect(toolbarMount).toBeGreaterThan(rfClose);
    expect(wb).toMatch(/className="relative flex-1 flex flex-col min-w-0"/);
  });
});
