/**
 * StackGradeBadge — the came-up-healthy grade pill (healthy / pending /
 * unhealthy) shared by the wizard's Done overview and the "edit an existing
 * stack" picker (both fed by the API's StackHealth DTO, CORE/API/schemas.py:3555
 * `health` on GET /stacks/{id} and the GET /stacks list). Absent grade ->
 * renders nothing (older deployments / stacks never observed).
 */

import React from "react";
import { render, screen } from "@testing-library/react";
import { StackGradeBadge } from "@/components/stack-wizard-panel";

// The badge is a bare <span>, so the wrapper div is the assertion surface:
// its only child carries the grade's colour + tooltip.
function badgeSpan(container: HTMLElement) {
  return container.querySelector("span") as HTMLElement | null;
}

describe("StackGradeBadge", () => {
  it("renders nothing when health is absent", () => {
    const first = render(<div>{StackGradeBadge(null)}</div>);
    expect(badgeSpan(first.container)).toBeNull();
    const second = render(<div>{StackGradeBadge(undefined)}</div>);
    expect(badgeSpan(second.container)).toBeNull();
  });

  it("renders nothing when the grade itself is empty", () => {
    const { container } = render(<div>{StackGradeBadge({ grade: "" })}</div>);
    expect(badgeSpan(container)).toBeNull();
  });

  it("renders green 'healthy' with a grade-only tooltip", () => {
    const { container } = render(<div>{StackGradeBadge({ grade: "healthy" })}</div>);
    expect(screen.getByText("healthy")).toBeTruthy();
    const span = badgeSpan(container);
    expect(span?.getAttribute("class")).toContain("bg-emerald-100 text-emerald-700");
    expect(span?.getAttribute("title")).toBe("Health grade: healthy");
  });

  it("renders amber 'pending' with the reason in the tooltip", () => {
    const { container } = render(
      <div>{StackGradeBadge({ grade: "pending", reason: "no recent observation" })}</div>
    );
    expect(screen.getByText("pending")).toBeTruthy();
    const span = badgeSpan(container);
    expect(span?.getAttribute("class")).toContain("bg-amber-100 text-amber-700");
    expect(span?.getAttribute("title")).toBe("Health grade: pending — no recent observation");
  });

  it("renders red 'unhealthy' with the reason in the tooltip", () => {
    const { container } = render(
      <div>{StackGradeBadge({ grade: "unhealthy", reason: "worker died" })}</div>
    );
    expect(screen.getByText("unhealthy")).toBeTruthy();
    const span = badgeSpan(container);
    expect(span?.getAttribute("class")).toContain("bg-red-100 text-red-700");
    expect(span?.getAttribute("title")).toBe("Health grade: unhealthy — worker died");
  });

  it("a null reason falls back to the grade-only tooltip", () => {
    const { container } = render(
      <div>{StackGradeBadge({ grade: "unhealthy", reason: null })}</div>
    );
    expect(badgeSpan(container)?.getAttribute("title")).toBe("Health grade: unhealthy");
  });
});
