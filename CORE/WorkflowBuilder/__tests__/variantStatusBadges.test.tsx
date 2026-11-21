/**
 * GO.9 — honest per-accelerator labels: status label/style mapping + badge
 * rendering for verified/unverified/incompatible/not_recommended, with the
 * optional perf ratio.
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import {
  VariantStatusBadge,
  VariantStatusBadges,
  variantStatusLabel,
  variantStatusStyle,
} from "@/components/node-chips";

describe("variantStatusLabel", () => {
  it("maps each known status to its display label", () => {
    expect(variantStatusLabel("verified")).toBe("Verified");
    expect(variantStatusLabel("unverified")).toBe("Unverified");
    expect(variantStatusLabel("incompatible")).toBe("Incompatible");
    expect(variantStatusLabel("not_recommended")).toBe("Not recommended");
  });

  it("falls back to the raw status string for an unknown status", () => {
    expect(variantStatusLabel("mystery")).toBe("mystery");
  });

  it("defaults to 'Unverified' for null/undefined", () => {
    expect(variantStatusLabel(null)).toBe("Unverified");
    expect(variantStatusLabel(undefined)).toBe("Unverified");
  });
});

describe("variantStatusStyle", () => {
  it("gives incompatible a distinct (red) style from verified (green)", () => {
    expect(variantStatusStyle("incompatible")).toContain("red");
    expect(variantStatusStyle("verified")).toContain("emerald");
    expect(variantStatusStyle("not_recommended")).toContain("amber");
    expect(variantStatusStyle("unverified")).toContain("slate");
  });

  it("falls back to the unverified style for an unknown status", () => {
    expect(variantStatusStyle("mystery")).toBe(variantStatusStyle("unverified"));
  });
});

describe("VariantStatusBadge", () => {
  it("renders variant name + status label", () => {
    render(<VariantStatusBadge variant="cuda" entry={{ status: "verified" }} />);
    expect(screen.getByText("CUDA: Verified")).toBeInTheDocument();
  });

  it("renders the perf ratio when present", () => {
    render(<VariantStatusBadge variant="npu" entry={{ status: "not_recommended", perf_ratio: 0.1 }} />);
    expect(screen.getByText("NPU: Not recommended · 0.10x")).toBeInTheDocument();
  });

  it("omits the ratio suffix when perf_ratio is null", () => {
    render(<VariantStatusBadge variant="rocm" entry={{ status: "unverified", perf_ratio: null }} />);
    expect(screen.getByText("ROCm: Unverified")).toBeInTheDocument();
  });

  it("sets data-variant-status for the e2e spec / styling hooks", () => {
    render(<VariantStatusBadge variant="cuda" entry={{ status: "verified" }} />);
    const badge = screen.getByText("CUDA: Verified");
    expect(badge).toHaveAttribute("data-variant", "cuda");
    expect(badge).toHaveAttribute("data-variant-status", "verified");
  });
});

describe("VariantStatusBadges", () => {
  it("renders one badge per variant, in verified/unverified/incompatible mix", () => {
    render(
      <VariantStatusBadges
        variantStatus={{
          cuda: { status: "verified", perf_ratio: 1.0 },
          rocm: { status: "unverified" },
          npu: { status: "incompatible" },
        }}
      />
    );
    expect(screen.getByText("CUDA: Verified · 1.00x")).toBeInTheDocument();
    expect(screen.getByText("ROCm: Unverified")).toBeInTheDocument();
    expect(screen.getByText("NPU: Incompatible")).toBeInTheDocument();
  });

  it("renders nothing when variantStatus is null/undefined/empty", () => {
    const { container: c1 } = render(<VariantStatusBadges variantStatus={null} />);
    expect(c1).toBeEmptyDOMElement();
    const { container: c2 } = render(<VariantStatusBadges variantStatus={undefined} />);
    expect(c2).toBeEmptyDOMElement();
    const { container: c3 } = render(<VariantStatusBadges variantStatus={{}} />);
    expect(c3).toBeEmptyDOMElement();
  });
});
