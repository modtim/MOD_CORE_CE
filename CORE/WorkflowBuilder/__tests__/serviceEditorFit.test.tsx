/**
 * WS4.33-product — the service builder's GPU/VRAM field shows a live
 * "does this fit the fleet?" verdict from POST /stacks/placement-check.
 *
 * The mock fetch stands in for the real endpoint; the component must:
 *   - render the red shortfall line when placeable=false,
 *   - render the green "Fits on …" line when placeable=true,
 *   - render NOTHING when the request fails (never block editing).
 */

import React from "react";
import { render, screen, fireEvent, act, waitFor } from "@testing-library/react";

let fetchImpl: (url: string, init?: RequestInit) => Promise<Response>;

jest.mock("@/lib/api-client", () => ({
  authenticatedFetch: (url: string, init?: RequestInit) => fetchImpl(url, init),
}));

import { ServiceEditor } from "@/components/service-builder/ServiceEditor";
import { ServiceSpec } from "@/components/service-builder/types";

const baseService: ServiceSpec = {
  name: "infer",
  source: { kind: "image", image: "ollama/ollama:latest" },
  env: {},
  ports: [],
  volumes: [],
  gpu: { vram_gb: 4 },
  replicas: 1,
  cpu: 2,
  mem_gb: 8,
  expose: false,
  command: null,
};

function jsonResponse(body: Record<string, unknown>) {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(body),
  } as unknown as Response);
}

function mount() {
  return render(<ServiceEditor service={baseService} onChange={() => {}} />);
}

function enableGpu(container: HTMLElement, vram: string) {
  // The buffer starts from props (gpu already set on baseService), so the
  // VRAM input is present; just edit its value.
  const input = container.querySelector(
    'input[type="number"][min="0"][step="1"][class*="max-w-[140px]"]',
  ) as HTMLInputElement;
  fireEvent.change(input, { target: { value: vram } });
}

beforeEach(() => {
  jest.useFakeTimers();
  fetchImpl = () => jsonResponse({});
});

afterEach(() => {
  jest.useRealTimers();
});

test("unplaceable verdict renders the shortfall line", async () => {
  fetchImpl = (url) =>
    jsonResponse({
      placeable: false,
      services: [{
        name: "infer",
        placeable: false,
        best_worker: null,
        shortfall: { needed_gb: 22.5, max_available_gb: 16, kind: "cuda" },
      }],
      message: "1 service(s) cannot be placed: infer",
    });
  const { container } = mount();
  enableGpu(container, "22.5");
  await act(async () => {
    jest.advanceTimersByTime(600);
  });
  await waitFor(() => {
    expect(screen.getByTestId("service-fit-shortfall")).toBeInTheDocument();
  });
  expect(screen.getByTestId("service-fit-shortfall").textContent).toContain(
    "No worker can place this: needs 22.5 GB",
  );
  expect(screen.getByTestId("service-fit-shortfall").textContent).toContain(
    "largest free is 16 GB (cuda)",
  );
});

test("placeable verdict renders the fits-on line", async () => {
  fetchImpl = () =>
    jsonResponse({
      placeable: true,
      services: [{
        name: "infer",
        placeable: true,
        best_worker: "mod-worker-big",
      }],
      message: "All services can be placed on the current fleet.",
    });
  const { container } = mount();
  enableGpu(container, "4");
  await act(async () => {
    jest.advanceTimersByTime(600);
  });
  await waitFor(() => {
    expect(screen.getByTestId("service-fit-ok")).toBeInTheDocument();
  });
  expect(screen.getByTestId("service-fit-ok").textContent).toContain(
    "Fits on mod-worker-big",
  );
});

test("a request error renders nothing and never blocks editing", async () => {
  fetchImpl = () => Promise.reject(new Error("network down"));
  const { container } = mount();
  enableGpu(container, "8");
  await act(async () => {
    jest.advanceTimersByTime(600);
  });
  expect(
    container.querySelector('[data-testid="service-fit-ok"]'),
  ).not.toBeInTheDocument();
  expect(
    container.querySelector('[data-testid="service-fit-shortfall"]'),
  ).not.toBeInTheDocument();
  // Editing still works while the lookup has failed.
  const input = container.querySelector(
    'input[type="number"][min="0"][step="1"][class*="max-w-[140px]"]',
  ) as HTMLInputElement;
  fireEvent.change(input, { target: { value: "999" } });
  expect(input.value).toBe("999");
});

test("no request goes out while the VRAM value is empty/zero", async () => {
  let calls = 0;
  fetchImpl = () => {
    calls++;
    return jsonResponse({ placeable: true, services: [], message: "" });
  };
  const { container } = mount();
  enableGpu(container, "0");
  await act(async () => {
    jest.advanceTimersByTime(600);
  });
  expect(calls).toBe(0);
});
