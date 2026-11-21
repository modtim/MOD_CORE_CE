/**
 * Stack wizard docker-compose import client contract (lib/compose-import.ts).
 *
 * Pins the pure glue the import step uses: seeding confirmations from the
 * preview's suggestions, computing which required needs still block finalize,
 * and building the finalize request body (raw YAML + pruned confirmations).
 */
import {
  buildFinalizeRequest,
  ComposeImportPreview,
  outstandingRequired,
  seedConfirmations,
} from "../lib/compose-import";

const preview: ComposeImportPreview = {
  name: "mystack",
  compose_spec: { services: { gpu: { image: "vllm/vllm-openai" }, db: { image: "postgres:16" } } },
  storage_class: "s3_native",
  needs_confirmation: [
    {
      scope: "service", service: "gpu", field: "accelerator.min_vram_gb",
      suggested: null, reason: "GPU detected; confirm VRAM", required: true,
    },
    {
      scope: "service", service: "db", field: "egress",
      suggested: { mode: "none" }, reason: "confirm egress", required: false,
      options: ["none", "api", "hosts"],
    },
    {
      scope: "service", service: "gpu", field: "readiness.path",
      suggested: "/", reason: "confirm health path", required: false,
    },
    {
      scope: "stack", field: "storage_class", suggested: "s3_native",
      reason: "confirm storage", required: false, options: ["s3_native", "local_fs"],
    },
    {
      scope: "stack", field: "ha_class", suggested: "external",
      reason: "confirm ha", required: false, options: ["stateless", "external"],
    },
  ],
};

describe("seedConfirmations", () => {
  it("seeds service and stack defaults from suggestions", () => {
    const c = seedConfirmations(preview);
    expect(c.services?.db?.egress).toEqual({ mode: "none" });
    expect(c.services?.gpu?.readiness).toEqual({ path: "/" });
    expect(c.storage_class).toBe("s3_native");
    expect(c.ha_class).toBe("external");
    // a null-suggested need (accelerator VRAM) seeds nothing
    expect(c.services?.gpu?.accelerator).toBeUndefined();
  });
});

describe("outstandingRequired", () => {
  it("reports the unanswered required accelerator VRAM", () => {
    const c = seedConfirmations(preview);
    const out = outstandingRequired(preview.needs_confirmation, c);
    expect(out.map((n) => n.field)).toEqual(["accelerator.min_vram_gb"]);
  });

  it("clears once the VRAM is provided", () => {
    const c = seedConfirmations(preview);
    c.services!.gpu = { ...(c.services!.gpu || {}), accelerator: { required: true, min_vram_gb: 48 } };
    expect(outstandingRequired(preview.needs_confirmation, c)).toHaveLength(0);
  });

  it("a zero/absent VRAM does not satisfy the requirement", () => {
    const c = seedConfirmations(preview);
    c.services!.gpu = { accelerator: { required: true, min_vram_gb: 0 } };
    expect(outstandingRequired(preview.needs_confirmation, c)).toHaveLength(1);
  });
});

describe("buildFinalizeRequest", () => {
  it("sends raw YAML and prunes empty service entries", () => {
    const c = seedConfirmations(preview);
    c.services!.gpu = { accelerator: { required: true, min_vram_gb: 48 }, readiness: { path: "/" } };
    const yaml = "services:\n  gpu:\n    image: vllm/vllm-openai\n";
    const req = buildFinalizeRequest("mystack", yaml, c);

    expect(req.name).toBe("mystack");
    expect(req.compose).toBe(yaml);
    expect(req.confirmations.services?.gpu?.accelerator).toEqual({ required: true, min_vram_gb: 48 });
    expect(req.confirmations.services?.db?.egress).toEqual({ mode: "none" });
    expect(req.confirmations.storage_class).toBe("s3_native");
    expect(req.confirmations.ha_class).toBe("external");
  });

  it("preserves an explicit null (clear the field)", () => {
    const req = buildFinalizeRequest("s", "services: {}", {
      services: { a: { accelerator: null } },
    });
    expect(req.confirmations.services?.a).toEqual({ accelerator: null });
  });

  it("drops services with no set fields", () => {
    const req = buildFinalizeRequest("s", "services: {}", { services: { a: {} } });
    expect(req.confirmations.services).toEqual({});
  });
});
