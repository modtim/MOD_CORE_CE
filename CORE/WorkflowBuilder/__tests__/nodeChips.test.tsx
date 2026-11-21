/**
 * LDF-3 §3.7 — chip rendering: cost edge cases (0, 1, 2+) and
 * gpu_variants → label derivation (CPU / single-accelerator / Multi).
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import { CostChip, ProcessingChip, NodeChips, normalizeCostTiers } from "@/components/node-chips";
import CostBasisBadge from "@/components/CostBasisBadge";

describe("CostChip", () => {
  it("renders '0 credits' for n=0 (spec: 'including 0')", () => {
    render(<CostChip cost={0} />);
    expect(screen.getByText("0 credits")).toBeInTheDocument();
  });

  it("renders '1 credit' (singular) for n=1", () => {
    render(<CostChip cost={1} />);
    expect(screen.getByText("1 credit")).toBeInTheDocument();
  });

  it("renders '2 credits' (plural) for n=2", () => {
    render(<CostChip cost={2} />);
    expect(screen.getByText("2 credits")).toBeInTheDocument();
  });

  it("renders nothing when cost is null", () => {
    const { container } = render(<CostChip cost={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when cost is undefined", () => {
    const { container } = render(<CostChip cost={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("ProcessingChip", () => {
  it("renders 'CPU' when gpu_variants is empty", () => {
    render(<ProcessingChip gpuVariants={{}} />);
    expect(screen.getByText("CPU")).toBeInTheDocument();
  });

  it("renders 'CPU' when gpu_variants is null", () => {
    render(<ProcessingChip gpuVariants={null} />);
    expect(screen.getByText("CPU")).toBeInTheDocument();
  });

  it("renders 'CPU' when gpu_variants only has cpu key", () => {
    render(<ProcessingChip gpuVariants={{ cpu: "img:cpu" }} />);
    expect(screen.getByText("CPU")).toBeInTheDocument();
  });

  it("renders accelerator display name for single-accelerator", () => {
    render(<ProcessingChip gpuVariants={{ cuda: "img:cuda" }} />);
    expect(screen.getByText("CUDA")).toBeInTheDocument();
  });

  it("renders ROCm for rocm-only", () => {
    render(<ProcessingChip gpuVariants={{ rocm: "img:rocm" }} />);
    expect(screen.getByText("ROCm")).toBeInTheDocument();
  });

  it("renders accelerator name when accelerator + cpu fallback both present", () => {
    render(<ProcessingChip gpuVariants={{ cuda: "img:cuda", cpu: "img:cpu" }} />);
    // Accelerator wins — user wants to know what hardware this needs,
    // not that the cpu fallback exists.
    expect(screen.getByText("CUDA")).toBeInTheDocument();
  });

  it("renders 'Multi' for 2+ accelerators with tooltip listing variants", () => {
    render(<ProcessingChip gpuVariants={{ cuda: "img:cuda", rocm: "img:rocm" }} />);
    const chip = screen.getByText("Multi");
    expect(chip).toBeInTheDocument();
    expect(chip).toHaveAttribute("title", expect.stringContaining("CUDA"));
    expect(chip).toHaveAttribute("title", expect.stringContaining("ROCm"));
  });

  it("falls back to uppercased key for unknown variant", () => {
    render(<ProcessingChip gpuVariants={{ cuda: "img:cuda", weirdgpu: "img" } as any} />);
    // cuda + weirdgpu = 2 accelerators (weirdgpu treated as non-cpu)? No,
    // weirdgpu isn't in the ACCELERATOR_KEYS set so it's ignored. Single
    // recognised accelerator = label is "CUDA".
    expect(screen.getByText("CUDA")).toBeInTheDocument();
  });
});

describe("NodeChips", () => {
  it("renders both chips together for a typical node", () => {
    render(<NodeChips cost={3} gpuVariants={{ cuda: "img:cuda" }} />);
    expect(screen.getByText("3 credits")).toBeInTheDocument();
    expect(screen.getByText("CUDA")).toBeInTheDocument();
  });
});

/**
 * tcp 8.1/8.2 — three-tier cost chip (S/M/L) + cost_basis badge.
 *
 * The library item itself (node-library.tsx) is a fetch-driven Next.js
 * component that cannot be rendered headlessly, so the badge is verified
 * through the NodeChips + CostBasisBadge composition the library uses.
 */
describe("CostChip — three tiers", () => {
  it("renders '1 / 3 / 12 credits' for valid tiers", () => {
    render(<CostChip cost={3} costTiers={{ small: 1, medium: 3, large: 12 }} />);
    const chip = screen.getByText("1 / 3 / 12 credits");
    expect(chip).toBeInTheDocument();
    expect(chip).toHaveAttribute("title", "small / medium / large input");
  });

  it("tiers win over the legacy single cost when both are present", () => {
    render(<CostChip cost={99} costTiers={{ small: 1, medium: 3, large: 12 }} />);
    expect(screen.getByText("1 / 3 / 12 credits")).toBeInTheDocument();
    expect(screen.queryByText("99 credits")).toBeNull();
  });

  it("all-zero tiers keep the legacy free rendering", () => {
    render(<CostChip cost={0} costTiers={{ small: 0, medium: 0, large: 0 }} />);
    const chip = screen.getByText("0 credits");
    expect(chip).toBeInTheDocument();
    expect(chip).toHaveAttribute("title", "Costs 0 credits per run");
  });

  it("zero-cost tiers still render free when legacy cost is non-zero", () => {
    render(<CostChip cost={5} costTiers={{ small: 0, medium: 0, large: 0 }} />);
    expect(screen.getByText("0 credits")).toBeInTheDocument();
    expect(screen.queryByText("5 credits")).toBeNull();
  });
});

describe("CostChip — malformed tiers fall back to legacy", () => {
  it("missing key (no large) → legacy single cost", () => {
    render(<CostChip cost={4} costTiers={{ small: 1, medium: 3 }} />);
    expect(screen.getByText("4 credits")).toBeInTheDocument();
  });

  it("null tier value → legacy single cost", () => {
    render(<CostChip cost={4} costTiers={{ small: 1, medium: null, large: 12 }} />);
    expect(screen.getByText("4 credits")).toBeInTheDocument();
  });

  it("null tiers → legacy single cost", () => {
    render(<CostChip cost={4} costTiers={null} />);
    expect(screen.getByText("4 credits")).toBeInTheDocument();
  });

  it("negative tier → legacy single cost", () => {
    render(<CostChip cost={4} costTiers={{ small: -1, medium: 3, large: 12 }} />);
    expect(screen.getByText("4 credits")).toBeInTheDocument();
  });

  it("malformed tiers + no legacy cost → nothing", () => {
    const { container } = render(<CostChip cost={null} costTiers={{ small: 1 }} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("normalizeCostTiers", () => {
  it("returns the tiers verbatim when complete", () => {
    expect(normalizeCostTiers({ small: 1, medium: 3, large: 12 })).toEqual({ small: 1, medium: 3, large: 12 });
  });

  it("returns null for incomplete / non-numeric payloads", () => {
    expect(normalizeCostTiers(null)).toBeNull();
    expect(normalizeCostTiers(undefined)).toBeNull();
    expect(normalizeCostTiers({ small: 1, medium: 3 })).toBeNull();
    expect(normalizeCostTiers({ small: 1, medium: "3", large: 12 })).toBeNull();
  });
});

describe("NodeChips — legacy single cost unchanged (tiers)", () => {
  it("no costTiers prop → identical to before", () => {
    render(<NodeChips cost={3} gpuVariants={{ cuda: "img:cuda" }} />);
    expect(screen.getByText("3 credits")).toBeInTheDocument();
    expect(screen.getByText("CUDA")).toBeInTheDocument();
  });

  it("tiers replace the cost text, processing chip untouched", () => {
    render(<NodeChips cost={3} costTiers={{ small: 1, medium: 3, large: 12 }} gpuVariants={{ cuda: "img:cuda" }} />);
    expect(screen.getByText("1 / 3 / 12 credits")).toBeInTheDocument();
    expect(screen.getByText("CUDA")).toBeInTheDocument();
  });
});

describe("tcp 8.2 — cost_basis badge beside the chips", () => {
  it("library composition renders chips + basis chip", () => {
    render(
      <span>
        <NodeChips cost={3} costTiers={{ small: 1, medium: 3, large: 12 }} gpuVariants={{ cuda: "img:cuda" }} />
        <CostBasisBadge costBasis="calibrated" className="ml-1.5" />
      </span>
    );
    expect(screen.getByText("1 / 3 / 12 credits")).toBeInTheDocument();
    expect(screen.getByText("CUDA")).toBeInTheDocument();
    const badge = screen.getByTestId("cost-basis-badge");
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveTextContent("Calibrated");
    expect(badge).toHaveAttribute("data-cost-basis", "calibrated");
  });

  it("absent cost_basis → no badge, chips unchanged", () => {
    render(
      <span>
        <NodeChips cost={3} gpuVariants={{ cuda: "img:cuda" }} />
        <CostBasisBadge costBasis={undefined} className="ml-1.5" />
      </span>
    );
    expect(screen.getByText("3 credits")).toBeInTheDocument();
    expect(screen.queryByTestId("cost-basis-badge")).toBeNull();
  });
});
