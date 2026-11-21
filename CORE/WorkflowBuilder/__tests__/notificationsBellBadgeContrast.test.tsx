/**
 * FX.68 p2b — the notifications-bell unread badge failed axe's WCAG2.aa
 * colour-contrast check: bg-red-500 #ef4444 with white text = 3.76:1
 * (needs >= 4.5:1). Fixed to bg-red-600 #dc2626, white text = 4.83:1.
 *
 * Two halves:
 *  1. DOM half — the real bell is mounted with a mocked fetch so the unread
 *     count lands and the badge actually renders; we assert the shipped
 *     class list on the badge span (bg-red-600, text-white, and NOT the
 *     old bg-red-500).
 *  2. Math half — the ratios are recomputed here with the WCAG relative-
 *     luminance formula so the pin can never drift from the numbers.
 *
 * Both themes: app/globals.css overrides neither `.dark .bg-red-600` nor
 * `.dark .text-white`, so the same #dc2626/white pair is what the browser
 * paints in light AND dark — the ratio below therefore holds in both.
 */
import * as fs from "fs";
import * as path from "path";
import React from "react";
import { render, screen, act } from "@testing-library/react";
import NotificationsBell from "@/components/notifications-bell";

/* --- WCAG 2.x relative luminance + contrast ratio (spec formula) -------- */
function srgbToLinear(channel255: number): number {
  const c = channel255 / 255;
  return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}
function relativeLuminance(hex: string): number {
  const r = srgbToLinear(parseInt(hex.slice(1, 3), 16));
  const g = srgbToLinear(parseInt(hex.slice(3, 5), 16));
  const b = srgbToLinear(parseInt(hex.slice(5, 7), 16));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function contrastRatio(a: string, b: string): number {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  const hi = Math.max(la, lb);
  const lo = Math.min(la, lb);
  return (hi + 0.05) / (lo + 0.05);
}

const RED_500 = "#ef4444";
const RED_600 = "#dc2626";
const WHITE = "#ffffff";

describe("notifications-bell unread badge — WCAG contrast math", () => {
  it("the old bg-red-500 + white pair really did fail 4.5:1 (3.76)", () => {
    expect(contrastRatio(RED_500, WHITE).toFixed(2)).toBe("3.76");
    expect(contrastRatio(RED_500, WHITE)).toBeLessThan(4.5);
  });

  it("the new bg-red-600 + white pair passes 4.5:1 (4.83)", () => {
    expect(contrastRatio(RED_600, WHITE).toFixed(2)).toBe("4.83");
    expect(contrastRatio(RED_600, WHITE)).toBeGreaterThanOrEqual(4.5);
  });

  it("globals.css leaves the pair alone in .dark (same colours both themes)", () => {
    const css = fs.readFileSync(
      path.resolve(__dirname, "../app/globals.css"),
      "utf-8",
    );
    expect(css).not.toMatch(/\.dark[^{}]*\.bg-red-600/);
    expect(css).not.toMatch(/\.dark[^{}]*\.text-white/);
  });
});

/* --- DOM half: the badge actually renders with the pinned classes ------ */
describe("notifications-bell unread badge — rendered classes", () => {
  const SRC = fs.readFileSync(
    path.resolve(__dirname, "../components/notifications-bell.tsx"),
    "utf-8",
  );

  it("shipped source uses bg-red-600 (not bg-red-500) on the .top-1 badge", () => {
    const badgeLine = SRC
      .split("\n")
      .find((line) => line.includes("top-1 right-1") && line.includes("rounded-full"));
    expect(typeof badgeLine).toBe("string");
    expect(badgeLine!).toContain("bg-red-600");
    expect(badgeLine!).toContain("text-white");
    expect(badgeLine!.includes("bg-red-500")).toBe(false);
  });

  it("renders the badge span with bg-red-600 text-white once unread > 0", async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ unread_count: 7 }),
    } as unknown as Response);
    const previousFetch = global.fetch;
    global.fetch = fetchMock as unknown as typeof fetch;

    const { unmount } = render(
      <NotificationsBell accessToken="tok" apiUrl="http://api.test" />,
    );
    try {
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
      // testing-library's `screen` root has no querySelectorAll, so query the
      // jsdom document the component rendered into.
      const badges = document.querySelectorAll("span[class*='top-1 right-1']");
      expect(badges).toHaveLength(1);
      const cls = badges[0].getAttribute("class") ?? "";
      expect(cls).toContain("bg-red-600");
      expect(cls).toContain("text-white");
      expect(cls.includes("bg-red-500")).toBe(false);
      expect(badges[0].textContent).toBe("7");
      unmount();
    } finally {
      global.fetch = previousFetch;
    }
  });
});
