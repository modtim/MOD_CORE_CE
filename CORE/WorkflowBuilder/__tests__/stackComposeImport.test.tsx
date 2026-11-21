/**
 * StackComposeImport (stack-wizard-compose-import).
 *
 * The component wraps two calls — POST /stacks/import-compose (preview) and
 * POST /stacks/import-compose/finalize — around the pure glue in
 * lib/compose-import.ts. These tests pin the wiring: the preview's needs
 * render, Save stays disabled until the required ones are answered, the
 * finalize body carries the raw compose + pruned confirmations, a 422 renders
 * inline instead of throwing, and a 200 finalize calls onCreated.
 */

import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StackComposeImport } from "@/components/stack-compose-import";
import { ComposeImportPreview } from "@/lib/compose-import";

const jsonResp = (status: number, body: unknown) =>
  ({ ok: status < 400, status, json: async () => body }) as unknown as Response;

const PREVIEW: ComposeImportPreview = {
  name: "mystack",
  compose_spec: { services: { gpu: { image: "vllm/vllm-openai" } } },
  storage_class: "local_fs",
  needs_confirmation: [
    {
      scope: "service",
      service: "gpu",
      field: "accelerator.min_vram_gb",
      suggested: null,
      reason: "GPU detected; confirm the required VRAM per device (GB)",
      required: true,
    },
    {
      scope: "service",
      service: "gpu",
      field: "egress",
      suggested: { mode: "none" },
      reason: "confirm the outbound network policy for this service",
      required: false,
      options: ["none", "api", "hosts"],
    },
  ],
};

const COMPOSE_YAML = "services:\n  gpu:\n    image: vllm/vllm-openai\n";

function fillPasteStage() {
  fireEvent.change(screen.getByLabelText("Stack name"), { target: { value: "mystack" } });
  fireEvent.change(screen.getByLabelText("docker-compose.yml"), {
    target: { value: COMPOSE_YAML },
  });
}

describe("StackComposeImport", () => {
  it("previews and lists the needs_confirmation items", async () => {
    const authFetch = jest.fn().mockResolvedValue(jsonResp(200, PREVIEW));
    render(<StackComposeImport authFetch={authFetch} onCreated={jest.fn()} />);

    fillPasteStage();
    fireEvent.click(screen.getByTestId("compose-import-preview"));

    await waitFor(() => expect(screen.getByTestId("compose-import-save")).toBeInTheDocument());
    expect(screen.getByText(/GPU detected; confirm the required VRAM/)).toBeInTheDocument();
    expect(screen.getByText(/confirm the outbound network policy/)).toBeInTheDocument();

    expect(authFetch).toHaveBeenCalledWith(
      "/stacks/import-compose",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ name: "mystack", compose: COMPOSE_YAML }),
      }),
    );
  });

  it("disables Save until the required need is answered, then finalizes with compose + confirmations", async () => {
    const authFetch = jest.fn().mockResolvedValue(jsonResp(200, PREVIEW));
    const onCreated = jest.fn();
    render(<StackComposeImport authFetch={authFetch} onCreated={onCreated} />);

    fillPasteStage();
    fireEvent.click(screen.getByTestId("compose-import-preview"));
    await waitFor(() => expect(screen.getByTestId("compose-import-save")).toBeInTheDocument());

    expect(screen.getByTestId("compose-import-save")).toBeDisabled();

    fireEvent.change(screen.getByTestId("need-service-gpu-accelerator.min_vram_gb"), {
      target: { value: "24" },
    });

    await waitFor(() => expect(screen.getByTestId("compose-import-save")).not.toBeDisabled());

    const createdStack = { id: 42, name: "mystack", status: "created" };
    authFetch.mockResolvedValueOnce(jsonResp(200, createdStack));
    fireEvent.click(screen.getByTestId("compose-import-save"));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(createdStack));

    const finalizeCall = authFetch.mock.calls.find((c) => c[0] === "/stacks/import-compose/finalize");
    expect(finalizeCall).toBeDefined();
    const body = JSON.parse((finalizeCall![1] as RequestInit).body as string);
    expect(body.compose).toBe(COMPOSE_YAML);
    expect(body.confirmations.services.gpu.accelerator.min_vram_gb).toBe(24);
    expect(body.confirmations.services.gpu.egress).toEqual({ mode: "none" });
  });

  it("renders a 422 detail inline instead of throwing", async () => {
    const authFetch = jest
      .fn()
      .mockResolvedValue(jsonResp(422, { detail: "unsupported compose: build, extends, secrets" }));
    render(<StackComposeImport authFetch={authFetch} onCreated={jest.fn()} />);

    fillPasteStage();
    fireEvent.click(screen.getByTestId("compose-import-preview"));

    await waitFor(() =>
      expect(screen.getByTestId("compose-import-error")).toHaveTextContent(
        "unsupported compose: build, extends, secrets",
      ),
    );
    expect(screen.queryByTestId("compose-import-save")).not.toBeInTheDocument();
  });

  it("409 requires_ack on finalize shows the warning, then confirming resends with acknowledge_no_backup", async () => {
    const authFetch = jest.fn().mockResolvedValue(jsonResp(200, PREVIEW));
    const onCreated = jest.fn();
    render(<StackComposeImport authFetch={authFetch} onCreated={onCreated} />);

    fillPasteStage();
    fireEvent.click(screen.getByTestId("compose-import-preview"));
    await waitFor(() => expect(screen.getByTestId("compose-import-save")).toBeInTheDocument());

    fireEvent.change(screen.getByTestId("need-service-gpu-accelerator.min_vram_gb"), {
      target: { value: "24" },
    });
    await waitFor(() => expect(screen.getByTestId("compose-import-save")).not.toBeDisabled());

    const ackDetail = {
      requires_ack: true,
      reason: "pg (postgres:15) — this stack runs a known database image with no backup posture declared.",
      services: [{ service: "pg", image: "postgres:15", engine: "postgres" }],
    };
    authFetch.mockResolvedValueOnce(jsonResp(409, { detail: ackDetail }));
    fireEvent.click(screen.getByTestId("compose-import-save"));

    await waitFor(() => expect(screen.getByTestId("compose-import-backup-ack")).toBeInTheDocument());
    expect(screen.getByText(/These databases have no backup plan: pg \(postgres:15\)/)).toBeInTheDocument();
    expect(onCreated).not.toHaveBeenCalled();

    // Confirm stays disabled until the checkbox is checked.
    expect(screen.getByTestId("compose-import-backup-ack-confirm")).toBeDisabled();
    fireEvent.click(screen.getByTestId("compose-import-backup-ack-checkbox"));
    await waitFor(() =>
      expect(screen.getByTestId("compose-import-backup-ack-confirm")).not.toBeDisabled(),
    );

    const createdStack = { id: 7, name: "mystack", status: "created" };
    authFetch.mockResolvedValueOnce(jsonResp(200, createdStack));
    fireEvent.click(screen.getByTestId("compose-import-backup-ack-confirm"));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(createdStack));

    const calls = authFetch.mock.calls.filter((c) => c[0] === "/stacks/import-compose/finalize");
    expect(calls).toHaveLength(2);
    const firstBody = JSON.parse((calls[0][1] as RequestInit).body as string);
    const secondBody = JSON.parse((calls[1][1] as RequestInit).body as string);
    expect(firstBody.acknowledge_no_backup).toBeUndefined();
    expect(secondBody.acknowledge_no_backup).toBe(true);
    // Identical request otherwise — same name/compose/confirmations.
    expect({ ...secondBody, acknowledge_no_backup: undefined }).toEqual(firstBody);
  });

  it("Cancel on the backup-ack warning discards it without re-posting", async () => {
    const authFetch = jest.fn().mockResolvedValue(jsonResp(200, PREVIEW));
    const onCreated = jest.fn();
    render(<StackComposeImport authFetch={authFetch} onCreated={onCreated} />);

    fillPasteStage();
    fireEvent.click(screen.getByTestId("compose-import-preview"));
    await waitFor(() => expect(screen.getByTestId("compose-import-save")).toBeInTheDocument());

    fireEvent.change(screen.getByTestId("need-service-gpu-accelerator.min_vram_gb"), {
      target: { value: "24" },
    });
    await waitFor(() => expect(screen.getByTestId("compose-import-save")).not.toBeDisabled());

    authFetch.mockResolvedValueOnce(
      jsonResp(409, {
        detail: {
          requires_ack: true,
          reason: "no backup posture declared.",
          services: [{ service: "pg", image: "postgres:15", engine: "postgres" }],
        },
      }),
    );
    fireEvent.click(screen.getByTestId("compose-import-save"));
    await waitFor(() => expect(screen.getByTestId("compose-import-backup-ack")).toBeInTheDocument());

    const callsBeforeCancel = authFetch.mock.calls.length;
    fireEvent.click(screen.getByTestId("compose-import-backup-ack-cancel"));

    await waitFor(() =>
      expect(screen.queryByTestId("compose-import-backup-ack")).not.toBeInTheDocument(),
    );
    expect(authFetch.mock.calls.length).toBe(callsBeforeCancel);
    expect(onCreated).not.toHaveBeenCalled();
  });

  it("renders a 422 from finalize and does not call onCreated", async () => {
    const authFetch = jest.fn().mockResolvedValue(jsonResp(200, PREVIEW));
    const onCreated = jest.fn();
    render(<StackComposeImport authFetch={authFetch} onCreated={onCreated} />);

    fillPasteStage();
    fireEvent.click(screen.getByTestId("compose-import-preview"));
    await waitFor(() => expect(screen.getByTestId("compose-import-save")).toBeInTheDocument());

    fireEvent.change(screen.getByTestId("need-service-gpu-accelerator.min_vram_gb"), {
      target: { value: "24" },
    });
    await waitFor(() => expect(screen.getByTestId("compose-import-save")).not.toBeDisabled());

    authFetch.mockResolvedValueOnce(jsonResp(422, { detail: "did not validate: bad readiness port" }));
    fireEvent.click(screen.getByTestId("compose-import-save"));

    await waitFor(() =>
      expect(screen.getByTestId("compose-import-error")).toHaveTextContent(
        "did not validate: bad readiness port",
      ),
    );
    expect(onCreated).not.toHaveBeenCalled();
  });
});
